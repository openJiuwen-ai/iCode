# OpenTelemetry reference

iCode can use OpenTelemetry to record the following telemetry data during agent runs:

- **Traces**: record the start and end times, parent-child relationships, and errors of operations such as agent runs, model requests, and tool calls, helping you inspect the call sequence and identify where time is spent.
- **Logs**: record the time, level, and details of individual events, such as successful tool calls, runtime warnings, or errors. When call context is available, logs can be correlated with the corresponding traces.
- **Metrics**: record numerical values such as model request duration, input and output token counts, and tool call duration, helping you summarize usage, duration distributions, and trends.

A collector is a service that receives telemetry data. iCode sends data to a collector using the OpenTelemetry Protocol (OTLP). On this page, an “endpoint” is the address where the collector receives telemetry data.

This page covers iCode OpenTelemetry configuration, local storage and export of telemetry data, and connections to OTLP collectors.

## Enabling telemetry and choosing a destination

### Enablement settings

OpenTelemetry is disabled by default. Enable it under [Settings → Safety → Telemetry](../guides/configuration/settings.md#telemetry) in the terminal user interface (TUI), or use the following environment variables. **All settings on this page take effect after restarting iCode.**

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `CHRYS_OTEL` | `false` | Enable OpenTelemetry export |
| `CHRYS_OTEL_ENDPOINT` | Empty | Set the collector endpoint; corresponds to “Telemetry endpoint” in the TUI |
| `CHRYS_OTEL_SENSITIVE_DATA` | `false` | Include sensitive content such as prompts, model responses, and tool arguments in telemetry |

Boolean values accept `1`, `true`, `yes`, and `on` (enabled), or `0`, `false`, `no`, and `off` (disabled), case-insensitively.

Settings changed in the TUI are saved in the user settings file, `settings.yaml`. When the environment variables above are set to valid values:

- Environment variables take precedence over the corresponding file settings without modifying the file.
- The TUI displays the values specified by the environment variables and disables editing of the corresponding settings.

When `CHRYS_OTEL` or “OpenTelemetry export” in the TUI is disabled, iCode neither saves nor sends telemetry data, even if an endpoint or sensitive data option is configured.

### Data destinations

When OpenTelemetry is enabled without any collector endpoint configured, traces and logs are saved locally. Configuring a collector endpoint switches to remote export.

Traces, logs, and metrics can share a receiving address or use separate addresses. [Endpoint precedence](#endpoint-precedence) determines which address each data type uses. Data destinations are as follows:

| Data type | Local storage | Remote export (a receiving address is configured for this data type) |
| --- | --- | --- |
| Traces | `otel/traces.jsonl` in the current session folder | Sent to the collector |
| Logs | `otel/logs.jsonl` in the current session folder | Sent to the collector |
| Metrics | Not saved | Sent to the collector |

Local files are created when the corresponding records are produced. For the session folder location, see [Find the session ID and storage location](../guides/daily-use/sessions.md#find-the-session-id-and-storage-location).

After you configure any collector endpoint and restart iCode, iCode stops saving telemetry data locally. Failed remote exports do not fall back to local storage, so the affected data may be lost.

Whether the receiving service saves data, and how long it retains it, depends on its configuration.

## Sensitive data

Enabling “Include sensitive data in telemetry” or setting `CHRYS_OTEL_SENSITIVE_DATA=true` may include prompts, model responses, tool arguments, and tool results in traces and logs. This content may contain file contents, credentials, or other private information. The setting affects both local storage and remote export.

**When sensitive data is enabled, tool call duration metrics also carry tool arguments.** These arguments may therefore be sent to the collector even if you export only metrics.

Before enabling this option, confirm the data storage location, access permissions, and retention policy. Disabling it does not delete records that have already been saved or sent.

## Connecting to a collector

Remote export requires a running OTLP collector that accepts OTLP/gRPC. Use the endpoint and authentication information provided by the collector when configuring the connection.

The standard `OTEL_*` variables below can also be set in a `.env` file in the working directory or in the user `.env` file (macOS / Linux: `~/.chrys/.env`; Windows: `%APPDATA%\chrys\.env`). At startup, values from these files replace variables of the same name exported in the shell.

### Endpoint precedence

The address shared by all three data types is called the **base endpoint**. Configure it through “Telemetry endpoint” in the TUI, `CHRYS_OTEL_ENDPOINT`, or the standard environment variable `OTEL_EXPORTER_OTLP_ENDPOINT`.

To specify separate receiving addresses, set the following **signal-specific endpoints**. A signal-specific endpoint overrides the base endpoint only for its corresponding data type:

| Data type | Signal-specific endpoint environment variable |
| --- | --- |
| Traces | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` |
| Logs | `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` |
| Metrics | `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` |

When multiple settings are present, each data type uses the first nonempty endpoint in this order:

1. The signal-specific endpoint environment variable for that data type.
2. The general environment variable `OTEL_EXPORTER_OTLP_ENDPOINT`.
3. `CHRYS_OTEL_ENDPOINT`.
4. “Telemetry endpoint” saved in the TUI.

If no base endpoint is configured and only one signal-specific endpoint is set, only that data type is exported. For example, setting only `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` sends only traces; logs and metrics are neither sent nor saved locally.

Standard `OTEL_EXPORTER_OTLP_*` environment variables do not appear in “Telemetry endpoint” in the TUI. If data still goes to the old address after you change the endpoint in the TUI, check for standard environment variables with higher precedence.

### Protocol and address format

iCode sends data over OTLP/gRPC and uses the configured endpoint addresses directly. Leave `OTEL_EXPORTER_OTLP_PROTOCOL` unset or set it to `grpc`. iCode installations do not include the OTLP HTTP exporter: with `http/protobuf` (or `http`), OpenTelemetry setup fails and iCode records no telemetry at all, not even locally; any other value exports nothing.

Include the scheme in the endpoint address: `http://` uses an unencrypted connection, suitable for a local collector; `https://` uses TLS. When connecting to a remote collector, use `https://` and configure authentication as required by the service.

### Authentication headers

If the collector requires authentication through request headers, use the following environment variables:

| Environment variable | Scope |
| --- | --- |
| `OTEL_EXPORTER_OTLP_HEADERS` | All data types |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | Traces |
| `OTEL_EXPORTER_OTLP_LOGS_HEADERS` | Logs |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` | Metrics |

Headers use comma-separated `name=value` pairs. Use lowercase header names, because gRPC rejects uppercase ones; values are sent as written, without URL decoding. For example:

```bash
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <token>"
```

Replace `<token>` with the token provided by the collector, and set it before starting iCode.

Signal-specific headers are merged with general headers. When both define a header with the same name, the signal-specific value takes precedence.

### Local connection example

Suppose a local collector accepts OTLP gRPC requests at `http://localhost:4317`, requires no authentication, and no other standard endpoint environment variables are set. Run the following in Bash or Zsh:

```bash
export CHRYS_OTEL=true
export CHRYS_OTEL_ENDPOINT=http://localhost:4317
icode
```

This configuration sends traces, logs, and metrics to `http://localhost:4317` over an unencrypted gRPC connection.

## Viewing data at the receiving service

### Service identity

The default service name (`service.name`) is `chrys`, and the service version is the currently installed iCode version. Use the service name to find data in the collector or its connected observability platform.

If iCode instances running in multiple locations send data to the same collector, use service names and resource attributes to distinguish their sources. Resource attributes are labels attached to telemetry data, such as the runtime environment and instance name.

For example, when running an iCode instance in a test environment, set the following in Bash or Zsh:

```bash
export OTEL_SERVICE_NAME=icode-test
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=staging,service.instance.id=test-01"
```

After you start iCode in the same terminal, exported data carries the following identifiers, which you can use to filter it in the observability platform:

| Attribute | Example value | Meaning |
| --- | --- | --- |
| `service.name` | `icode-test` | Service name |
| `deployment.environment.name` | `staging` | Runtime environment; here, a test environment |
| `service.instance.id` | `test-01` | Instance name, used to distinguish multiple iCode instances in the same environment |

`OTEL_RESOURCE_ATTRIBUTES` accepts multiple comma-separated `name=value` pairs. These attributes override default attributes with the same names. A `service.name` attribute also overrides `OTEL_SERVICE_NAME`.

For example, setting `OTEL_SERVICE_NAME=icode-test` together with `OTEL_RESOURCE_ATTRIBUTES="service.name=icode-qa"` results in the service name `icode-qa`.

### Metrics and verification

When a metrics endpoint is configured (including one inherited from the base endpoint), iCode attempts to export the following metrics every 5 seconds:

| Metric | Unit | Measurement |
| --- | --- | --- |
| `gen_ai.client.operation.duration` | Seconds | Model request duration |
| `gen_ai.client.token.usage` | Tokens | Input and output token counts for model requests, recorded separately |
| `chrys.function.invocation.duration` | Seconds | Tool call duration |

iCode automatically adds the `gen_ai.token.type` label to token usage. When querying `gen_ai.client.token.usage` in the observability platform:

- Filter by `gen_ai.token.type=input` to view input token usage.
- Filter by `gen_ai.token.type=output` to view output token usage.
- Group by `gen_ai.token.type` to display both types of usage together.

With OpenTelemetry enabled and a receiving address configured for metrics, submit a request that triggers a model request or tool call. Then query the metrics above at the receiving service to confirm that data was exported successfully. Token usage is recorded only when the model response provides the corresponding statistics.

If no data arrives, check the following in order:

1. OpenTelemetry is enabled, and iCode was restarted after the configuration changed.
2. The collector accepts OTLP/gRPC at the endpoint, `OTEL_EXPORTER_OTLP_PROTOCOL` is unset or `grpc`, and no endpoint setting with higher precedence is present.
3. Whether the collector requires authentication, and whether the authentication headers are configured correctly.
