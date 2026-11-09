# 设计：定制环境配置与上游 Settings 解耦（environment-config）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-09
- 确认意见：无特别意见

## 1. 现状与决策依据

当前唯一进入上游 Settings 的 fork 私有配置是 `model.catalog.base_url`：

- `foundation/config/settings.py` 持有 label 与 `model_catalog_base_url` 字段/spec（C032）；
- `foundation/config/process_settings.py` 持有投影（C033）；
- `app/tui/screens/settings/layout.py` 将该键列入隐藏项（C039）；
- pot/po/mo 属标记豁免生成物；i18n oracle 的 id 集合与计数分别登记为 C042/C043。

`AIXCODING_AUTH_ENVIRONMENT` 同时被 auth 与模型目录（model catalog）使用，名称与真实语义不符。
未来内部 update/telemetry 已确定需要同一 `local/dev/prod` 部署环境，因此本批次立即统一，不再等待
第三个消费方落地后才处理。

**架构事实**：`tests/architecture/test_layering.py` 禁止 kernel→kernel 绝对导入，ruff TID252
禁止父级相对导入。`kernel/aixcoding/auth` 与 `kernel/aixcoding/diagnostics` 不能直接引用新建的
`kernel/aixcoding/config.py`。放 foundation 又违反 ADR-0011 的定制落位原则。因此采用
**service 共享读取器 + app 组合注入**，不修改架构规则。

## 2. 范围

### 做

1. 新增 `AIXCODING_ENVIRONMENT` 与 `aixcoding.toml` 顶层 `environment`；
2. 新建 `service/aixcoding/config/` 子包，统一读取并缓存共享配置；
3. app 启动时向 auth 注入环境、向 diagnostics 传递日志配置；
4. 模型目录 base URL 迁至 `[model_catalog].base_url`；
5. 删除上游 Settings 私有键及其投影、UI、i18n 跟改；
6. 删除全部旧变量兼容与检测，包括模型目录现有退役 WARNING；
7. 为未来 update/telemetry 固定配置契约，但不实现字段。

### 不做

- 不抽 tier 地址注册表、公共 HTTP 层、通用工具层；
- 不引入全局 origin/base_url；
- 不改 `AIXCODING_AUTH_SERVER_URL`、`AIXCODING_MODEL_CATALOG_URL/TOKEN/SYNC_INTERVAL`；
- 不改 auth 端点表、模型目录端点表、token/userId 来源、登录和同步行为；
- 不实现 `[update]`、`[telemetry]` 的解析或消费；
- 不支持项目级配置、热加载、旧变量别名或退役提示。

## 3. 配置契约

`~/.chrys/aixcoding.toml`：

```toml
environment = "prod"

[logging]
level = "INFO"
panel = false

[model_catalog]
# base_url = "http://82.187.34.98/csas"
```

### 3.1 环境解析

优先级：

```text
AIXCODING_ENVIRONMENT > TOML environment > "prod"
```

细则：

- 值经 `strip().lower()` 后必须属于 `local/dev/prod`；
- 环境变量已设置但为空或非法时，直接回退 prod 并输出 WARNING，不再回落 TOML；
- 环境变量未设置时，才读取 TOML；TOML 值非法同样回退 prod 并输出 WARNING；
- WARNING 走 stderr 的 `aixcoding.config` 提示，沿用现有配置解析 fail-safe 通道；
- 文件缺失或没有 `environment` 键是正常情况，不告警；
- TOML 整体语法错误时，logging/模型目录使用默认值，环境变量仍然有效；无环境变量则回退 prod。

### 3.2 模型目录端点

```text
AIXCODING_MODEL_CATALOG_URL（完整端点，最高优先）
> [model_catalog].base_url + CATALOG_PATH
> environment 对应的 _CATALOG_BASES[environment] + CATALOG_PATH
```

`[model_catalog].base_url` 仅接受字符串并去首尾空白；非字符串回退为空并提示。非空但 URL 不可用时，
不静默改连 prod：沿用现有同步失败、保留旧模型目录的降级路径。

### 3.3 auth 端点

auth 保留 `_BASE_PATHS` 和 `_replace_origin()`：

```text
environment 对应的 (auth_url, data_url)
> 如设置 AIXCODING_AUTH_SERVER_URL，仅替换两者 origin，路径保持不变
```

`AIXCODING_AUTH_SERVER_URL` 不影响模型目录、update/telemetry。

### 3.4 生命周期与安全边界

- `load_config()` 返回进程内缓存快照，重启生效；
- 不支持项目级 `.aixcoding.toml`；
- token/userId 不进入 TOML；
- `AIXCODING_MODEL_CATALOG_TOKEN` 与 auth session 的现有优先级不变；
- 环境变量、旧 YAML 键均不做退役检测。

## 4. 模块设计

### 4.1 `service/aixcoding/config/`（新增子包）

职责唯一：读取并缓存 `aixcoding.toml`，解析 `environment`、`logging`、`model_catalog` 三段。

```text
service/aixcoding/config/
├── __init__.py
├── loader.py
└── types.py
```

- `types.py` 持有 `ModelCatalogConfig` 与 `AixcodingConfig`；复用 kernel diagnostics 的
  `DiagnosticsConfig` 类型，依赖方向为 service→kernel，合法；
- `loader.py` 负责文件读取、校验、构造 `LoggingConfig` / `DiagnosticsConfig` 与环境解析，并持有唯一的
  `@cache load_config(config_dir: Path | None = None) -> AixcodingConfig`；
- `__init__.py` 只重导出 `load_config`、`AixcodingConfig`、`ModelCatalogConfig`，保持消费方使用
  `chrys.service.aixcoding.config`，不建立第二份缓存或解析入口。

逻辑结构：

```text
ModelCatalogConfig(base_url: str)
AixcodingConfig(
    environment: str,
    diagnostics: DiagnosticsConfig,
    model_catalog: ModelCatalogConfig,
)
@cache load_config(config_dir: Path | None = None) -> AixcodingConfig
```

- `config_dir=None` 时由 `loader.py` lazy import `get_platform().config_dir`；
- `@cache` 使 app 两次消费与模型目录后续消费共享同一快照；测试以重导出函数的
  `load_config.cache_clear()` 隔离；
- 文件缺失、坏 TOML、坏类型均返回 fail-safe 配置，不向启动调用方抛异常；
- 不读取 `settings.yaml`，不 import 上游 `process_settings()`；
- 不实现 update/telemetry dataclass 或字段。

不再单独新建 `environment.py`：环境字符串及解析均在 `loader.py`，避免为单一配置读取器拆出多余层次。

### 4.2 `app/aixcoding/startup/`（定制文件迁为子包）

```text
app/aixcoding/startup/
├── __init__.py
└── tasks.py
```

- `tasks.py` 持有 `_configure_environment()` 与 `_ensure_file_sink()` 两个任务实现；
- `__init__.py` 重导出任务函数，持有 `_TASKS` 与 `aixcoding_startup()`；既有上游
  `from chrys.app.aixcoding.startup import aixcoding_startup` 和测试的
  `monkeypatch.setattr(startup, "_TASKS", ...)` 均保持不变；
- 现有 `startup.py` 的组合与容错逻辑移至包入口，不新增第二套启动执行器。

启动任务调整为：

```text
_configure_environment()  -> configure_environment(load_config().environment)
_ensure_file_sink()       -> ensure_file_sink(load_config().diagnostics)
_TASKS = (_configure_environment, _ensure_file_sink)
```

- 两个任务共享 `@cache` 快照，文件只解析一次；
- 顺序固定：先环境注入，再日志装配；
- 每个任务继续由 `aixcoding_startup()` 独立 containment，失败不阻断 CLI；
- `app/cli/app.py::main()` 的既有唯一上游 import 与调用不变，仍早于 TUI/headless/ACP 分发；仅 C004
  行尾注释中的消费者路径改为 `src/chrys/app/aixcoding/startup/__init__.py`；
- 启动顺序测试必须证明环境注入先于默认 `LoginSession` 构造；placement 测试改按包入口
  `__init__.py` 定位，并继续覆盖 `_TASKS` 注入与任务注册。

### 4.3 `kernel/aixcoding/auth/environments.py`

保留 `Environment` 枚举于 `auth/types.py`，不把类型迁到 service。新增进程默认值注入：

```text
ENVIRONMENT_VARIABLE = "AIXCODING_ENVIRONMENT"
configure_environment(raw: str | None)
```

- `configure_environment(raw)` 将 service 已校验的字符串映射为 `Environment`；`None` 用于测试复位；
- `resolve_environment(environ=None)`：生产路径若已有注入值，直接返回注入值；显式传入 `environ`
  映射时绕过注入值并解析 `AIXCODING_ENVIRONMENT`，保证单元测试确定、隔离；
- 未经过 app 组合的直接 kernel 调用仍可读新环境变量并回退 prod，但不承诺读取 TOML；
- `resolve_endpoints()` 必须把调用方原始的 `environ`（包括 `None`）传给 `resolve_environment()`，
  不能先把 `None` 归一成 `os.environ` 再传，否则会被误判为显式映射而绕过注入；URL 构造与
  `AIXCODING_AUTH_SERVER_URL` 覆盖逻辑不变。

默认 `LoginSession` 一旦构造便固定 environment，因此禁止在启动注入前创建进程级单例；本设计不通过
重建单例掩盖顺序错误。

### 4.4 diagnostics

`kernel/aixcoding/diagnostics/config.py`：

- 保留 `LoggingConfig`、`DiagnosticsConfig` 及默认值；
- 删除文件读取与 `load_config()`，改由 service 共享模块构造这些类型。

`logging_support.py`：

- `ensure_file_sink(config: DiagnosticsConfig, config_dir=None)` 改为接收已解析配置；
- 不再自行读取 TOML；`setup_logging()` 主体、幂等、fail-safe 行为保持不变；
- 日志目录、主卷/detail 卷和 F6 panel 行为不变。

### 4.5 `service/aixcoding/models/catalog.py`（移动）

- 现有 `service/aixcoding/catalog.py` 移至 `service/aixcoding/models/catalog.py`，新增
  `service/aixcoding/models/__init__.py`；TUI 消费方 import、测试 import 与 monkeypatch 路径同步，并运行
  `uv run python -m tests.support.trajectory_wait_inventory` 机械刷新 wait manifest；
- 模型目录通过 service 同层绝对导入 `chrys.service.aixcoding.config` 读取共享配置，不触发
  kernel 跨子包限制；
- `catalog_base_url()` 改读 `load_config().model_catalog.base_url`；
- `catalog_tier()` 改读 `load_config().environment`；
- 删除 `process_settings().model_catalog_base_url` 读取；
- 删除 `RETIRED_ENV_NAMES`、`_warn_retired_env()` 及其在 URL、凭据、轮询周期入口的调用；
- 不再检测或警告任何 `CHRYS_MODEL_CATALOG_*` / `CHRYS_REMOTE_MODEL_CONFIG_URL`；
- 其余拉取、校验、发布、轮询、登录联动逻辑不动；函数名不做大规模改名，模块本身无独立 C id。

## 5. 上游文件改动（R2 报批）

| # | 文件 | 改动 | 登记处置 |
|---|---|---|---|
| 1 | `src/chrys/foundation/config/settings.py` | 删除模型目录 base label 与 `model_catalog_base_url` 字段/spec | C032 退役 |
| 2 | `src/chrys/foundation/config/process_settings.py` | 删除同名字段与投影 | C033 退役 |
| 3 | `src/chrys/app/tui/screens/settings/layout.py` | 删除 `model.catalog.base_url` 隐藏键 | C039 退役 |
| 4 | `locales/chrys.pot`、`locales/zh-Hans/LC_MESSAGES/chrys.po`、`src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | i18n 流水线删除模型目录 base label | 标记豁免生成物，无独立 C id |
| 5 | `tests/foundation/i18n/_catalog_oracle_ids.py`、`test_catalog_oracle.py` | 删除 id、更新计数 | C042/C043 退役 |
| 6 | `src/chrys/app/cli/app.py` | import 与调用不变；C004 行尾注释的消费者路径改为 `startup/__init__.py` | C004 保留，无新增 marker |
| 7 | `src/chrys/app/tui/app.py` | 模型目录 lazy import 改为 `chrys.service.aixcoding.models.catalog`，1 行；其余接线不变 | C034 保留，无新增 marker |
| 8 | `tests/architecture/trajectory_wait_manifest.json` | 运行 `uv run python -m tests.support.trajectory_wait_inventory` 机械刷新模块键及内部引用 | 生成物，无独立 C id |

不新增上游 marker；`app/cli/app.py` 仅同步 C004 行尾路径注释，`app/tui/app.py` 仅同步 C034 import 路径，
不改 registry/models UI/auth LLM 注入。

删除声明后，旧 `model.catalog.base_url` 会进入上游既有 `unknown_keys` 机制：上游注释明确其
“reported once, not dropped”，写入时保留未命名键。该行为是上游通用降级兼容，不是本批次新增的
定制检测；本批次不读取、不告警、不迁移该旧键。

## 定制点登记预告

本批次无新增上游定制点。C004 继续由 auth-logging 的现役登记持有，C034 继续由 catalog-sync 的现役登记
持有，二者 marker 锚与预期计数均不变；本批次仅更新 C004 括号内消费者路径与 C034 import 路径，不回改
auth-logging 历史 design。以下 id 从 catalog-sync 的现役登记移入本批次退役记录；原 design 保留指向说明
并由责任人按 R10 一并重签。

### 退役记录

| id | 原落点 | 退役日期 | 依据 |
|---|---|---|---|
| C032 | src/chrys/foundation/config/settings.py | 2026-11-09 | ADR-0022：fork 私有模型目录 base URL 退出上游 Settings，改由 `aixcoding.toml` 承载 |
| C033 | src/chrys/foundation/config/process_settings.py | 2026-11-09 | 同 C032：process projection 随字段删除 |
| C039 | src/chrys/app/tui/screens/settings/layout.py | 2026-11-09 | 同 C032：Settings 键删除后无需再隐藏 |
| C042 | tests/foundation/i18n/_catalog_oracle_ids.py | 2026-11-09 | 同 C032：模型目录 base label 从 oracle id 集合删除 |
| C043 | tests/foundation/i18n/test_catalog_oracle.py | 2026-11-09 | 同 C032：对应 borrow-shell 计数断言随 label 删除而退役 |

新 id 分配：无；高水位不因退役复用。最终 strict 对账须在本 design 与原 catalog-sync design 均签认、
代码 marker 同步删除后达到 0 error / 0 warning。

## 7. 旧配置处置

| 旧配置 | 新行为 |
|---|---|
| `AIXCODING_AUTH_ENVIRONMENT` | 不读取、不检测、不告警 |
| `CHRYS_AUTH_ENVIRONMENT` | 不读取、不检测、不告警 |
| `CHRYS_MODEL_CATALOG_BASE_URL` | 不读取、不检测、不告警 |
| `CHRYS_MODEL_CATALOG_URL/TOKEN/SYNC_INTERVAL` | 不读取、不检测、不告警；删除既有 WARNING |
| `CHRYS_REMOTE_MODEL_CONFIG_URL` | 不读取、不检测、不告警；删除既有 WARNING |
| `settings.yaml model.catalog.base_url` | 不再参与模型目录解析；仅服从上游 unknown-key 通用行为 |
| 0.28 `aixcoding.yaml` / `AIXCODING_EXTENSION_*` | 不恢复、不读取、不检测 |

## 8. 未来 update/telemetry 契约（仅文档）

```toml
[update]
# base_url = "..."

[telemetry]
# base_url = "..."
# enabled = true
# tool_param_mode = "full"  # full | whitelist
```

- 两者复用顶层 `environment`，服务路径各自持有；
- 如需覆盖，使用服务专用配置，不引入全局 origin/base_url；
- token/userId 由 auth session 提供；
- 上游 `app.update_check` 与 `otel.*` 不因本契约改变；
- 本批次不解析这些 section；消费批次若改变键名或语义，须新 ADR。

旧字段映射因此闭合：`profile` → `environment`；`customServerUrl` → 服务专用 origin/base_url；
`token/userId` → auth session；`telemetryEnabled` → `[telemetry].enabled`；`toolParamMode` →
`[telemetry].tool_param_mode`。

## 9. 文档联动

- `DIRECTORY.md` §9：登记 `AIXCODING_ENVIRONMENT`，移除模型目录 base 的 settings 例外实例，写明零兼容；
- `ROADMAP.md`：平台连接配置行指向本批次；F 类公共层行仅核销配置/环境解析子集；
- `INDEX.md`：仅登记本批次静态导航；签认状态由本文头部承载，不写入 INDEX（ADR-0016）；
- `aixcoding/mock/README.md` 与 mock 联调配方：`AIXCODING_AUTH_ENVIRONMENT=local` 改为
  `AIXCODING_ENVIRONMENT=local`；
- `aixcoding/docs/install-uninstall-support.md`：补充新环境变量与 TOML 配置位置；
- `catalog-sync/design.md`：保留退役预告，实施时删除五个现役行并由责任人一并重签；
- 批次 verification 记录旧配置失效行为与两次 local 手动验证，不宣称存在运行时迁移提示。

## 10. 测试与验证

1. **共享配置**：缺文件、完整配置、坏 TOML、坏类型、env > TOML > prod、非法值 WARNING、缓存一致性；
   包入口重导出与 `load_config.cache_clear()` 隔离有效。
2. **启动组合**：任务顺序、单快照、失败 containment、幂等、环境注入先于默认 LoginSession；包入口
   import、既有 `startup._TASKS` patch、任务注册与 placement 路径断言同步验证。
3. **auth**：三档端点零漂移、server URL origin 覆盖不变、新变量生效、旧变量完全无效、注入复位隔离。
4. **模型目录**：完整 URL > TOML base > tier base；路径拼接；坏 base 保留旧目录；旧变量无效果且无警告；
   token/interval/login/sync 既有测试全绿。
5. **drift**：`_CATALOG_BASES` 键集合、共享合法环境值、`auth.Environment` 三者一致。
6. **diagnostics**：现有 level/panel、主卷/detail 卷、F6、容错测试全绿。
7. **上游**：Settings/process_settings/layout/i18n/oracle 相关测试跟改并通过。
8. **架构**：`tests/architecture` 全量；无新增豁免、无 kernel 跨子包 import。
9. **工具**：定制测试全量、Smart Test（所有改动文件）、ruff/format/ty、i18n check、check_docs、
   `gen_customizations.py --strict`。
10. **手动**：以 `AIXCODING_ENVIRONMENT=local` 启动 mock 与 TUI，验证登录和模型目录同步；再用仅
    TOML `environment="local"` 验证一次。旧变量场景只做无网络单测，不触碰真实 prod。

## 11. 风险与回滚

- **旧脚本静默落到 prod**：零兼容的已接受后果；发布说明、mock README、支持手册同步新变量。
- **提前构造 LoginSession**：以启动顺序测试锚定；发现后调整调用顺序，不通过注入后重建会话修复。
- **缓存导致测试串扰**：相关测试 autouse 清理 `load_config` cache、auth 注入值和默认 session。
- **TOML 坏配置**：fail-safe 不阻断启动；非法 environment 必须可观测，显式坏模型目录 base 不静默改连 prod。
- **删除登记中间态**：本 design 未签认时退役表不参与对账，原 design 现役行先保留指向说明；实施完成
  并以两份签认 design 对账前，不提交严格登记验证结果。
- **回滚**：revert 本批次代码与文档即可恢复 Settings 字段和旧读取链；不迁移或改写用户文件，旧配置
  内容仍保留在原文件中。
