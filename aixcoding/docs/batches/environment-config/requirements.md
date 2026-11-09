# 需求：定制环境配置与上游 Settings 解耦（environment-config）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景

当前定制配置大部分已独立于上游 Settings，唯一存量耦合点是模型目录（model catalog）：

- `model.catalog.base_url` 声明于上游 `foundation/config/settings.py`；
- 环境变量 `CHRYS_MODEL_CATALOG_BASE_URL` 依赖上游 Settings 声明；
- `process_settings.py` 持有对应投影；
- settings UI 需要额外隐藏该键，i18n 与 oracle 也承担机械跟改。

同时，`AIXCODING_AUTH_ENVIRONMENT` 虽名为 auth，实际已被模型目录用作部署环境来源。未来内部 update 与 telemetry 也需要同一 `local/dev/prod` 部署语义。继续使用 auth 命名会让后续服务依赖错误概念；继续把服务配置加入上游 Settings，则会扩大上游改动面。

责任人已裁决：现在就统一规划并实施，不等 update/telemetry 批次；部署变量命名为 `AIXCODING_ENVIRONMENT`；旧配置零向后兼容；本批次同时删除模型目录既有 `CHRYS_MODEL_CATALOG_*` 退役名检测与 WARNING。

## 目标

1. 以 `AIXCODING_ENVIRONMENT` 统一表示 AIxCoding 内网服务部署环境，取值 `local/dev/prod`，默认 `prod`。
2. 以 `~/.chrys/aixcoding.toml` 作为定制层共享配置基座，支持顶层 `environment` 与 `[model_catalog].base_url`，保持现有 `[logging]` 行为。
3. 将模型目录的 base URL 配置从上游 `settings.yaml` / Settings 声明体系迁至 `aixcoding.toml`。
4. auth 与模型目录使用同一部署环境语义，但保留各自端点表和服务专用覆盖变量。
5. 采用符合 ADR-0011 与架构门禁的依赖形态：service 提供共享配置快照，app 启动组合入口向 auth/diagnostics 注入，模型目录直接消费同一快照。
6. 从上游 Settings、process projection、settings UI 和 i18n 中移除模型目录私有键。
7. 旧配置不读取、不检测、不告警；删除模型目录现有退役变量检测机制。

## 非目标

1. 不实现 update 或 telemetry 功能；仅在 ADR/design 固定其未来配置接入契约。
2. 不引入全局 origin、全局 base_url 或统一 tier 地址注册表。
3. 不提取公共 HTTP 封装、通用工具层或其他 ROADMAP F 类公共基础层。
4. 不改变 auth 的 `AIXCODING_AUTH_SERVER_URL`、委托登录变量、token 存储与 LLM 注入行为。
5. 不改变模型目录的完整 URL/token/轮询周期变量、登录联动、目录发布、registry 快照和只读 UI。
6. 不恢复 0.28 的 `aixcoding.yaml`，不在配置文件中保存 token 或 userId。
7. 不支持项目级配置覆盖，不支持会话中热切换部署环境。
8. 不为旧变量或旧 YAML 键提供别名、双读、退役检测或运行时迁移提示。

## 配置契约

目标文件 `~/.chrys/aixcoding.toml`：

```toml
environment = "prod"

[logging]
level = "INFO"
panel = false

[model_catalog]
# base_url = "http://82.187.34.98/csas"
```

部署环境优先级：

```text
AIXCODING_ENVIRONMENT > aixcoding.toml 顶层 environment > prod
```

模型目录端点优先级：

```text
AIXCODING_MODEL_CATALOG_URL（完整端点）
> aixcoding.toml [model_catalog].base_url + 固定 dispatch 路径
> environment 对应的模型目录 tier base + 固定 dispatch 路径
```

auth 端点仍由 environment 对应的 auth tier 表决定；`AIXCODING_AUTH_SERVER_URL` 仅覆盖 auth/user-data 的 origin 并保留各自路径。

## 验收标准

1. 设置 `AIXCODING_ENVIRONMENT=local` 后，auth 与模型目录均选择 local 端点；未设置任何配置时两者默认 prod。
2. 仅设置 TOML 顶层 `environment` 时，app 启动后 auth 使用该环境，模型目录使用同一环境；环境变量优先于 TOML。
3. 非法 environment 回退 prod 并产生可观测 WARNING；旧变量存在时没有任何读取、检测或警告。
4. `[model_catalog].base_url` 能替代旧 `model.catalog.base_url`；`AIXCODING_MODEL_CATALOG_URL` 仍具有最高优先级。
5. 删除上游 Settings 声明后，旧 `model.catalog.base_url` 不再生效；上游按既有 unknown-key 机制处理该键，定制代码不做额外检测。
6. `RETIRED_ENV_NAMES`、`_warn_retired_env()` 及其调用和测试全部移除，旧 `CHRYS_MODEL_CATALOG_*` 名称不再产生任何定制警告。
7. diagnostics 的 `[logging] level/panel` 行为保持；共享配置缺文件、坏 TOML、坏类型时 fail-safe，不阻断 CLI 启动。
8. app 启动组合发生在进程级 `LoginSession` 首次构造之前；TUI、headless、ACP 等经 `main()` 的入口均满足该顺序。
9. 依赖方向通过 `tests/architecture`：无 kernel 跨子包绝对导入、无向上 tier 依赖、无新增 DAG 豁免。
10. catalog-sync 的 C032/C033/C039/C042/C043 完成退役登记，无新增上游 marker；`gen_customizations.py --strict` 最终 0 error / 0 warning。
11. 定制测试、受影响上游测试、Smart Test、i18n、文档检查均通过；手动 mock 配方改用 `AIXCODING_ENVIRONMENT=local` 后登录与模型目录同步正常。

## 风险

1. **旧脚本静默换环境**：旧 `AIXCODING_AUTH_ENVIRONMENT=local` 不再生效且不会提示，程序按新默认规则运行，可能落到 prod。这是责任人明确接受的零兼容后果。
2. **启动顺序破坏**：若未来代码在 `aixcoding_startup()` 前构造默认 `LoginSession`，TOML 环境不会生效。需以启动顺序测试锚定。
3. **配置快照分裂**：若 auth 与模型目录分别读文件，可能得到不同环境。必须以 service 的进程内缓存快照保证一致。
4. **错误显式 base_url 误回退**：显式但不可用的模型目录 base_url 不应静默改连 prod；同步失败并保留旧目录。
5. **上游删除面漏项**：Settings 字段、projection、UI 隐藏、i18n/oracle 必须同步删除，否则出现死配置或生成物门禁失败。
6. **范围膨胀为公共层重构**：本批次只共享配置与环境解析，不抽地址注册表或网络封装。
