# OpenTelemetry 参考

iCode 可通过 OpenTelemetry 记录智能体运行过程中产生的以下遥测数据：

- **追踪**：记录智能体运行、模型请求和工具调用等操作的起止时间、父子调用关系及错误信息，用于查看调用过程和定位耗时环节。
- **日志**：按事件记录时间、级别和具体内容，例如工具调用成功、运行警告或错误信息；有调用上下文时，可与对应追踪关联。
- **指标**：记录模型请求耗时、输入与输出 Token 数量、工具调用耗时等数值，用于汇总用量、耗时分布和变化趋势。

收集器是接收遥测数据的服务。iCode 使用 OpenTelemetry Protocol（OTLP）向收集器发送数据。下文中的“端点”指收集器接收遥测数据的地址。

本页介绍 iCode 的 OpenTelemetry 配置、遥测数据的保存与导出方式，以及连接 OTLP 收集器的方法。

## 启用与数据去向

### 启用配置

OpenTelemetry 默认关闭。可在终端用户界面（Terminal User Interface，TUI）的[设置 → 安全管控 → 遥测](../guides/configuration/settings.md#遥测)中启用，也可使用以下环境变量。**本页配置均在重启 iCode 后生效。**

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CHRYS_OTEL` | `false` | 启用 OpenTelemetry 导出 |
| `CHRYS_OTEL_ENDPOINT` | 空 | 设置收集器端点，对应 TUI 的“遥测端点” |
| `CHRYS_OTEL_SENSITIVE_DATA` | `false` | 在遥测中包含提示词、模型回复和工具参数等敏感内容 |

布尔值接受 `1`、`true`、`yes`、`on`（开启）和 `0`、`false`、`no`、`off`（关闭），不区分大小写。

在 TUI 中修改的设置会保存在用户设置文件 `settings.yaml` 中。上述环境变量设置为有效值后：

- 环境变量优先于文件中的对应配置生效，但不会修改该文件。
- TUI 会显示环境变量指定的值，并禁用对应设置项的编辑。

关闭 `CHRYS_OTEL` 或 TUI 中的“OpenTelemetry 导出”后，即使已配置端点或敏感数据选项，iCode 也不会保存或发送遥测数据。

### 数据去向

启用 OpenTelemetry 后，如果未配置任何收集器端点，追踪和日志保存在本地；配置收集器端点后，则改为远端导出。

追踪、日志和指标可以共用一个接收地址，也可以分别配置。每类数据使用哪个地址，由[端点优先级](#端点优先级)决定；数据去向如下：

| 数据类型 | 本地保存 | 远端导出（已为该类数据配置接收地址） |
| --- | --- | --- |
| 追踪 | 当前会话文件夹中的 `otel/traces.jsonl` | 发送到收集器 |
| 日志 | 当前会话文件夹中的 `otel/logs.jsonl` | 发送到收集器 |
| 指标 | 不保存 | 发送到收集器 |

本地文件会在产生相应记录后创建。会话文件夹的位置见[查找会话 ID 和会话保存位置](../guides/daily-use/sessions.md#查找会话-id-和会话保存位置)。

配置任一收集器端点并重启 iCode 后，iCode 将停止在本地保存遥测数据。远端导出失败时不会回退到本地保存，因此相关数据可能丢失。

发送到接收端的数据是否保存以及保留多久，由接收端的配置决定。

## 敏感数据

开启“遥测中包含敏感数据”或设置 `CHRYS_OTEL_SENSITIVE_DATA=true` 后，追踪和日志中可能包含提示词、模型回复、工具参数和工具结果。这些内容可能包含文件内容、凭据或其他私密信息。该设置同时影响本地保存和远端导出。

**开启敏感数据后，工具调用耗时指标也会附带工具参数。** 因此，即使仅导出指标，也可能向收集器发送这些参数。

启用此选项前，请确认数据保存位置、访问权限和保留策略。关闭此选项不会删除此前已经保存或发送的记录。

## 连接收集器

远端导出需要已运行且接收 OTLP/gRPC 的 OTLP 收集器。配置时，请使用收集器提供的端点和认证信息。

下文的标准 `OTEL_*` 变量也可以写在工作目录的 `.env` 文件或用户 `.env` 文件（macOS / Linux：`~/.chrys/.env`；Windows：`%APPDATA%\chrys\.env`）中。启动时，这些文件中的值会覆盖 shell 中导出的同名变量。

### 端点优先级

三类数据共用的地址称为**基础端点**，可通过 TUI 中的“遥测端点”、`CHRYS_OTEL_ENDPOINT` 或标准环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 配置。

如需分别指定接收地址，可设置以下**类型专用端点**。类型专用端点仅覆盖对应数据类型的基础端点：

| 数据类型 | 专用端点环境变量 |
| --- | --- |
| 追踪 | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` |
| 日志 | `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` |
| 指标 | `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` |

存在多项配置时，每类数据按照以下优先级选择第一个非空端点：

1. 对应数据类型的专用端点环境变量。
2. 通用环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT`。
3. `CHRYS_OTEL_ENDPOINT`。
4. TUI 中保存的“遥测端点”。

如果未配置基础端点，仅设置某一类数据的专用端点，则只导出该类数据。例如，仅设置 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 时，只发送追踪数据；日志和指标既不会发送，也不会保存到本地。

标准 `OTEL_EXPORTER_OTLP_*` 环境变量不会显示在 TUI 的“遥测端点”中。如果修改 TUI 中的端点后，数据仍发送到原地址，请检查是否设置了优先级更高的标准环境变量。

### 协议与地址格式

iCode 通过 OTLP/gRPC 发送数据，直接使用配置的端点地址。`OTEL_EXPORTER_OTLP_PROTOCOL` 应保持未设置或设为 `grpc`。iCode 安装包不包含 OTLP HTTP 导出器：设为 `http/protobuf`（或 `http`）时，OpenTelemetry 初始化失败，iCode 不会记录任何遥测数据，也不会保存到本地；设为其他值时不会导出任何数据。

端点地址须包含协议前缀：`http://` 使用未加密连接，适用于本机收集器；`https://` 使用 TLS。连接远端收集器时，请使用 `https://`，并按服务要求配置认证信息。

### 认证请求头

如果收集器需要通过请求头进行认证，可使用以下环境变量：

| 环境变量 | 作用范围 |
| --- | --- |
| `OTEL_EXPORTER_OTLP_HEADERS` | 所有数据类型 |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | 追踪 |
| `OTEL_EXPORTER_OTLP_LOGS_HEADERS` | 日志 |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` | 指标 |

请求头使用逗号分隔的 `名称=值` 格式。请求头名称须使用小写字母，因为 gRPC 会拒绝包含大写字母的名称；值按原样发送，不做 URL 解码。例如：

```bash
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <token>"
```

将 `<token>` 替换为收集器提供的令牌，并在启动 iCode 前设置。

类型专用请求头会与通用请求头合并；存在同名请求头时，类型专用值优先。

### 本机连接示例

假设本机收集器通过 `http://localhost:4317` 接收 OTLP gRPC 请求，不需要认证，且未设置其他标准端点环境变量，可在 Bash 或 Zsh 中运行：

```bash
export CHRYS_OTEL=true
export CHRYS_OTEL_ENDPOINT=http://localhost:4317
icode
```

此配置会通过未加密的 gRPC 连接将追踪、日志和指标发送到 `http://localhost:4317`。

## 在接收端查看数据

### 服务标识

默认服务名（`service.name`）为 `chrys`，服务版本为当前安装的 iCode 版本。可在收集器或其连接的观测平台中按服务名查找数据。

如果多处运行的 iCode 向同一个收集器发送数据，可以用服务名和资源属性区分数据来源。资源属性是附在遥测数据上的标签，例如运行环境和实例名称。

例如，在测试环境中运行一个 iCode 实例，可在 Bash 或 Zsh 中设置：

```bash
export OTEL_SERVICE_NAME=icode-test
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=staging,service.instance.id=test-01"
```

在同一终端启动 iCode 后，导出的数据会带上以下标识，可在观测平台中据此筛选：

| 属性 | 示例值 | 含义 |
| --- | --- | --- |
| `service.name` | `icode-test` | 服务名称 |
| `deployment.environment.name` | `staging` | 运行环境，此处表示测试环境 |
| `service.instance.id` | `test-01` | 实例名称，用于区分同一环境中的多个 iCode 实例 |

`OTEL_RESOURCE_ATTRIBUTES` 使用逗号分隔多个 `名称=值`。其中的属性会覆盖同名默认属性；如果设置了 `service.name`，也会覆盖 `OTEL_SERVICE_NAME`。

例如，设置 `OTEL_SERVICE_NAME=icode-test`，同时设置 `OTEL_RESOURCE_ATTRIBUTES="service.name=icode-qa"`，最终服务名为 `icode-qa`。

### 指标与验证

配置指标端点后（也可继承基础端点），iCode 每 5 秒尝试导出以下指标：

| 指标 | 单位 | 统计内容 |
| --- | --- | --- |
| `gen_ai.client.operation.duration` | 秒 | 模型请求耗时 |
| `gen_ai.client.token.usage` | Token | 模型请求的输入与输出 Token 数量，分别记录 |
| `chrys.function.invocation.duration` | 秒 | 工具调用耗时 |

iCode 会为 Token 用量自动添加 `gen_ai.token.type` 标签。在观测平台查询 `gen_ai.client.token.usage` 时：

- 筛选 `gen_ai.token.type=input`，查看输入 Token 用量。
- 筛选 `gen_ai.token.type=output`，查看输出 Token 用量。
- 按 `gen_ai.token.type` 分组，同时展示两类用量。

已启用 OpenTelemetry 且为指标配置了接收地址时，可提交一次请求并触发模型请求或工具调用，然后在接收端查询上述指标，以确认数据是否成功导出。Token 用量仅在模型响应提供相应统计时记录。

如果未收到数据，可依次检查：

1. 是否已启用 OpenTelemetry，并在修改配置后重启 iCode。
2. 收集器是否在该端点接收 OTLP/gRPC，`OTEL_EXPORTER_OTLP_PROTOCOL` 是否未设置或设为 `grpc`，以及是否存在优先级更高的端点配置。
3. 收集器是否需要认证，以及认证请求头是否配置正确。
