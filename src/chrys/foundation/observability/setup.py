# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""OpenTelemetry setup for Chrys.

Exporter construction from the standard OTel env vars, the resource, and the
three SDK providers are built here. The chrys telemetry layers
(``chrys.kernel.instrumentation``) gate on :data:`TELEMETRY_GATE`, flipped via
:func:`~chrys.foundation.observability.gate.configure_telemetry` on setup success.

Env var ownership
-----------------
* **Standard OTel spec vars** (``OTEL_EXPORTER_OTLP_ENDPOINT``,
  ``OTEL_EXPORTER_OTLP_PROTOCOL``, ``OTEL_EXPORTER_OTLP_HEADERS`` and their
  per-signal variants, plus ``OTEL_SERVICE_NAME``/``OTEL_SERVICE_VERSION``/
  ``OTEL_RESOURCE_ATTRIBUTES``) are parsed by ``_get_exporters_from_env``
  and ``_create_resource``.
* **Chrys toggles** stay under the ``CHRYS_OTEL*`` namespace
  (``Settings.otel_enabled`` / ``Settings.otel_sensitive_data`` /
  ``Settings.otel_endpoint``).

Exporter fallback
-----------------
* If an OTLP endpoint is set (``OTEL_EXPORTER_OTLP_ENDPOINT`` or a
  signal-specific variant, or ``Settings.otel_endpoint`` as the base when
  the standard variable is unset), OTLP exporters ship spans/logs/metrics
  there.
* Otherwise, session-scoped JSONL file exporters write to
  ``{session_dir}/otel/{traces,logs}.jsonl`` once the engine activates a
  session — the "zero config, just works" default, safe in the TUI.

MCP telemetry
-------------
MCP client spans are emitted by the Chrys-owned MCP engine
(``service/mcp/owned.py``) and gate directly on :data:`TELEMETRY_GATE`, the same
switch used by chat, agent, and function spans.

Provider configuration and lifecycle:

- **Resource naming**: ``service.name`` defaults to ``"chrys"`` and
  ``service.version`` to the installed chrys version; the standard env
  overrides win.
- **Root-logger handler**: the OTel ``LoggingHandler`` goes on the **root**
  logger, so chrys INFO events (e.g. the sensitive-data message events) reach
  the logs sink.
- **Revocable lifecycle**: every ``setup_otel`` call first revokes whatever a
  previous call attached (gate, sink, root handler, ``chrys`` logger level),
  so a disabled/failed re-run after a successful one ends fully off instead
  of exporting through stale state. Only the global SDK providers persist
  (the OTel ``set_*_provider`` API is set-once per process), which also
  means re-*enabling* after a successful run is unsupported.

A :class:`chrys.foundation.observability.sink.OtelSessionSink` is installed as a
process-wide singleton so the engine can flip session state without
threading the sink through constructors.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from types import NoneType
from typing import TYPE_CHECKING, Any

from chrys import __version__
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.observability.gate import configure_telemetry
from chrys.foundation.observability.sink import OtelSessionSink, get_otel_sink, set_otel_sink

if TYPE_CHECKING:
    from chrys.foundation.config.settings import Settings

logger = logging.getLogger(__name__)

# Standard OTel env vars that, if any are set, indicate the user wants
# their data shipped over OTLP.  In that case we skip the file exporter
# fallback to avoid double-writes.
_OTLP_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)

_SERVICE_NAME_DEFAULT = "chrys"
_SERVICE_VERSION_DEFAULT = __version__


# Revocable state installed by a successful setup_otel run, tracked so a
# later disabled/failed re-run can tear it back down.  The OTel global
# providers themselves are process-permanent — the API ``set_*_provider``
# calls are set-once (a second set warns and keeps the first) — so a re-run
# can only revoke what chrys attached on top of them: the root
# ``LoggingHandler``, the raised ``chrys`` logger level, the telemetry gate,
# and the sink singleton.
_INSTALLED_ROOT_HANDLER: logging.Handler | None = None
_INSTALLED_LOGGER_PROVIDER: Any = None
_INSTALLED_TRACER_PROVIDER: Any = None
_PRIOR_CHRYS_LOGGER_LEVEL: int | None = None
_OTEL_ANY_VALUE_BODY_TYPES = (NoneType, bool, bytes, int, float, str, Sequence, Mapping)


def _base_endpoint(settings_endpoint: str = "") -> str | None:
    """The base OTLP endpoint: the standard env var, else the chrys setting."""
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or settings_endpoint.strip() or None


def _otlp_endpoint_configured(settings_endpoint: str = "") -> bool:
    return _base_endpoint(settings_endpoint) is not None or any(
        os.environ.get(v, "").strip() for v in _OTLP_ENDPOINT_VARS
    )


def _parse_headers(header_str: str) -> dict[str, str]:
    """Parse comma-separated key=value headers, trimming whitespace and ignoring unpaired entries."""
    headers: dict[str, str] = {}
    if not header_str:
        return headers
    for pair in header_str.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            headers[key.strip()] = value.strip()
    return headers


def _create_otlp_exporters(
    *,
    protocol: str = "grpc",
    traces_endpoint: str | None = None,
    traces_headers: dict[str, str] | None = None,
    metrics_endpoint: str | None = None,
    metrics_headers: dict[str, str] | None = None,
    logs_endpoint: str | None = None,
    logs_headers: dict[str, str] | None = None,
) -> list[Any]:
    """Build exporters for the configured signals and selected transport.

    Endpoints and headers are already resolved per signal. The OTLP HTTP
    exporter package is not a dependency: without it, the HTTP transport
    raises ``ImportError`` with an installation hint.
    """
    exporters: list[Any] = []

    if not logs_endpoint and not traces_endpoint and not metrics_endpoint:
        return exporters

    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter as GRPCLogExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as GRPCMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GRPCSpanExporter,
        )

        if logs_endpoint:
            exporters.append(GRPCLogExporter(endpoint=logs_endpoint, headers=logs_headers or None))
        if traces_endpoint:
            exporters.append(GRPCSpanExporter(endpoint=traces_endpoint, headers=traces_headers or None))
        if metrics_endpoint:
            exporters.append(GRPCMetricExporter(endpoint=metrics_endpoint, headers=metrics_headers or None))

    elif protocol in ("http/protobuf", "http"):
        try:
            # Version-pinned private exporter modules resolve at runtime but do not ship type stubs.
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter as HTTPLogExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter as HTTPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as HTTPSpanExporter,
            )
        except ImportError as exc:
            # AIxCoding branding: OTLP error message
            raise ImportError(
                f"OTLP over HTTP needs opentelemetry-exporter-otlp-proto-http, which {APP_DISPLAY_NAME} does not include; "
                "use OTLP/gRPC instead (leave OTEL_EXPORTER_OTLP_PROTOCOL unset or set it to grpc)"
            ) from exc

        if logs_endpoint:
            exporters.append(HTTPLogExporter(endpoint=logs_endpoint, headers=logs_headers or None))
        if traces_endpoint:
            exporters.append(HTTPSpanExporter(endpoint=traces_endpoint, headers=traces_headers or None))
        if metrics_endpoint:
            exporters.append(HTTPMetricExporter(endpoint=metrics_endpoint, headers=metrics_headers or None))

    return exporters


def _get_exporters_from_env(settings_endpoint: str = "") -> list[Any]:
    """Resolve OTLP endpoints and headers from environment and Chrys settings.

    ``settings_endpoint`` (``Settings.otel_endpoint``) stands in for the base
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` when that variable is not set.

    Per the OTel spec, ``OTEL_EXPORTER_OTLP_ENDPOINT`` is a *base* URL for
    HTTP — the SDK auto-appends ``/v1/{traces,metrics,logs}`` when it reads
    the env var itself. Because the values are read here and forwarded as the
    ``endpoint=`` constructor argument (always treated as a full URL), the
    auto-append is replicated for HTTP; signal-specific endpoint vars are
    full URLs used verbatim, and for gRPC the base endpoint is used as-is.
    """
    base_endpoint = _base_endpoint(settings_endpoint)
    traces_endpoint_specific = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    metrics_endpoint_specific = os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    logs_endpoint_specific = os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")

    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()

    traces_endpoint: str | None
    metrics_endpoint: str | None
    logs_endpoint: str | None
    if protocol in ("http/protobuf", "http") and base_endpoint:
        base_for_http = base_endpoint.rstrip("/")
        traces_endpoint = traces_endpoint_specific or f"{base_for_http}/v1/traces"
        metrics_endpoint = metrics_endpoint_specific or f"{base_for_http}/v1/metrics"
        logs_endpoint = logs_endpoint_specific or f"{base_for_http}/v1/logs"
    else:
        traces_endpoint = traces_endpoint_specific or base_endpoint
        metrics_endpoint = metrics_endpoint_specific or base_endpoint
        logs_endpoint = logs_endpoint_specific or base_endpoint

    base_headers = _parse_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""))
    traces_headers = {**base_headers, **_parse_headers(os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""))}
    metrics_headers = {**base_headers, **_parse_headers(os.getenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", ""))}
    logs_headers = {**base_headers, **_parse_headers(os.getenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", ""))}

    return _create_otlp_exporters(
        protocol=protocol,
        traces_endpoint=traces_endpoint,
        traces_headers=traces_headers or None,
        metrics_endpoint=metrics_endpoint,
        metrics_headers=metrics_headers or None,
        logs_endpoint=logs_endpoint,
        logs_headers=logs_headers or None,
    )


def _create_resource() -> Any:
    """Create the OTel resource with Chrys defaults and environment overrides.

    ``service.name`` defaults to ``"chrys"`` (override: ``OTEL_SERVICE_NAME``)
    and ``service.version`` to the installed chrys version (override:
    ``OTEL_SERVICE_VERSION``); ``OTEL_RESOURCE_ATTRIBUTES`` merges on top.
    """
    from opentelemetry.sdk.resources import Resource

    resource_attributes: dict[str, Any] = {
        "service.name": os.getenv("OTEL_SERVICE_NAME", _SERVICE_NAME_DEFAULT),
        "service.version": os.getenv("OTEL_SERVICE_VERSION", _SERVICE_VERSION_DEFAULT),
    }
    if resource_attrs_env := os.getenv("OTEL_RESOURCE_ATTRIBUTES"):
        resource_attributes.update(_parse_headers(resource_attrs_env))
    return Resource.create(resource_attributes)


def _create_logging_handler(logger_provider: Any) -> logging.Handler:
    """Build the replacement OTel handler while preserving the retired body semantics."""
    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    class _ChrysLoggingHandler(LoggingHandler):
        def _translate(self, record: logging.LogRecord) -> Any:
            translated = super()._translate(record)
            if (
                self.formatter is None
                and not record.args
                and not isinstance(record.msg, str)
                and isinstance(record.msg, _OTEL_ANY_VALUE_BODY_TYPES)
            ):
                # The retired SDK handler retained valid AnyValue bodies when
                # no %-formatting was requested. The replacement handler calls
                # getMessage() unconditionally, which stringifies structured
                # MCP log payloads; restore the previous wire shape.
                translated.body = record.msg
            return translated

    return _ChrysLoggingHandler(logger_provider=logger_provider, log_code_attributes=True)


def _configure_providers(exporters: list[Any]) -> None:
    """Configure one provider per signal kind.

    Providers are built only for the exporter kinds actually present — the
    JSONL fallback ships span+log exporters and no metric exporter, so no
    ``MeterProvider`` is installed in that mode. The OTel ``LoggingHandler``
    is attached to the *root* logger (see module docstring), and the
    handler/providers are tracked in the module revocation state so
    :func:`_revoke_previous_setup` can tear them back down.
    """
    global _INSTALLED_ROOT_HANDLER, _INSTALLED_LOGGER_PROVIDER, _INSTALLED_TRACER_PROVIDER

    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import MetricExporter, PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

    span_exporters: list[SpanExporter] = []
    log_exporters: list[LogRecordExporter] = []
    metric_exporters: list[MetricExporter] = []
    resource = _create_resource()
    for exp in exporters:
        if isinstance(exp, SpanExporter):
            span_exporters.append(exp)
        if isinstance(exp, LogRecordExporter):
            log_exporters.append(exp)
        if isinstance(exp, MetricExporter):
            metric_exporters.append(exp)

    if span_exporters:
        tracer_provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(tracer_provider)
        for exporter in span_exporters:
            tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        _INSTALLED_TRACER_PROVIDER = tracer_provider

    if log_exporters:
        logger_provider = LoggerProvider(resource=resource)
        for log_exporter in log_exporters:
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        handler = _create_logging_handler(logger_provider)
        logging.getLogger().addHandler(handler)
        # Track immediately after attaching — if a later step in this
        # function raises, the failure-path revocation must already see the
        # handler or it leaks on the root logger.
        _INSTALLED_ROOT_HANDLER = handler
        _INSTALLED_LOGGER_PROVIDER = logger_provider
        set_logger_provider(logger_provider)

    if metric_exporters:
        meter_provider = MeterProvider(
            metric_readers=[
                PeriodicExportingMetricReader(exporter, export_interval_millis=5000) for exporter in metric_exporters
            ],
            resource=resource,
        )
        metrics.set_meter_provider(meter_provider)


def _revoke_previous_setup() -> None:
    """Tear down everything a previous :func:`setup_otel` call installed that
    *can* be revoked: close the telemetry gate, flush and forget the tracked
    providers, detach the root ``LoggingHandler``, restore the ``chrys``
    logger level, and deactivate/clear the sink.  The global SDK providers
    stay installed (set-once API, see the revocation-state comment above) but
    lose every chrys write path, so nothing chrys-side is exported or written
    to a stale session after this returns.
    """
    global _INSTALLED_ROOT_HANDLER, _INSTALLED_LOGGER_PROVIDER, _INSTALLED_TRACER_PROVIDER
    global _PRIOR_CHRYS_LOGGER_LEVEL

    configure_telemetry(enabled=False)

    # Flush in-flight batch data from the enabled window before tearing the
    # write paths down — otherwise the tail would land in a deactivated
    # sink's in-memory buffer and be lost.
    for provider in (_INSTALLED_TRACER_PROVIDER, _INSTALLED_LOGGER_PROVIDER):
        if provider is not None:
            try:
                provider.force_flush(timeout_millis=5000)
            except Exception:
                logger.exception("Failed to flush an OTel provider while revoking telemetry setup")
    _INSTALLED_TRACER_PROVIDER = None
    _INSTALLED_LOGGER_PROVIDER = None

    if _INSTALLED_ROOT_HANDLER is not None:
        logging.getLogger().removeHandler(_INSTALLED_ROOT_HANDLER)
        _INSTALLED_ROOT_HANDLER.close()
        _INSTALLED_ROOT_HANDLER = None

    if _PRIOR_CHRYS_LOGGER_LEVEL is not None:
        logging.getLogger("chrys").setLevel(_PRIOR_CHRYS_LOGGER_LEVEL)
        _PRIOR_CHRYS_LOGGER_LEVEL = None

    stale_sink = get_otel_sink()
    set_otel_sink(None)
    if stale_sink is not None:
        stale_sink.deactivate()


def setup_otel(settings: Settings) -> OtelSessionSink | None:
    """Enable OpenTelemetry instrumentation when ``settings.otel_enabled``.

    Returns the installed :class:`OtelSessionSink` on success, or
    ``None`` if instrumentation is disabled or setup failed.  The engine
    retrieves the sink via :func:`chrys.foundation.observability.sink.get_otel_sink`
    and calls ``activate`` / ``deactivate`` at session boundaries.

    Call once during application startup, after loading settings and
    before constructing the ``AgentEngine``.  The chrys-attached telemetry
    state — the gate (:data:`chrys.foundation.observability.gate.TELEMETRY_GATE`), the sink
    singleton, the root ``LoggingHandler``, and the raised ``chrys`` logger
    level — is re-derived authoritatively on every call: everything a prior
    call installed is revoked at entry and the gate reopens only when every
    step below succeeds, so a disabled or failed (re-)run after a successful
    one ends fully off.  The global SDK providers are set-once per process,
    so *re-enabling* after a previous successful call is unsupported (the
    span pipeline keeps the first run's providers).
    """
    _revoke_previous_setup()

    if not settings.otel_enabled:
        return None

    # Choose exporter strategy: OTLP if the user set any endpoint env var or
    # the endpoint setting, else fall back to session-scoped JSONL files
    # under {session_dir}/otel/.
    use_file_fallback = not _otlp_endpoint_configured(settings.otel_endpoint)
    sink = OtelSessionSink(write_files=use_file_fallback)

    try:
        # Exporter/processor classes live in a separate module that imports
        # from opentelemetry.sdk, which loads only once telemetry is enabled.
        from chrys.foundation.observability.exporters import (
            SessionContextSpanProcessor,
            SessionJsonlLogExporter,
            SessionJsonlSpanExporter,
        )

        file_exporters: list[Any] = (
            [SessionJsonlSpanExporter(sink), SessionJsonlLogExporter(sink)] if use_file_fallback else []
        )
        # Configure env-derived OTLP exporters before session file exporters.
        exporters = [*_get_exporters_from_env(settings.otel_endpoint), *file_exporters]
        _configure_providers(exporters)
    except Exception:
        logger.exception("OpenTelemetry setup failed. Check the OTEL_EXPORTER_OTLP_* env vars.")
        # _configure_providers may have partially succeeded before a later
        # step raised (e.g. the root LoggingHandler already installed) —
        # revoke the state freshly tracked by THIS call, not just the entry
        # revocation of earlier calls.
        _revoke_previous_setup()
        return None

    # Install the session-context processor on the (now-configured) TracerProvider
    # so every span picks up chrys.session_id + gen_ai.conversation.id.
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        add_processor = getattr(provider, "add_span_processor", None)
        if add_processor is not None:
            add_processor(SessionContextSpanProcessor(sink))
        else:
            logger.warning(
                "TracerProvider (%s) does not accept custom span processors — "
                "chrys.session_id / gen_ai.conversation.id will not be injected. "
                "This usually means no exporters were configured (no OTLP endpoint, "
                "no file fallback) — check OTEL_EXPORTER_OTLP_* env vars.",
                type(provider).__name__,
            )
    except Exception:
        logger.exception("Failed to install chrys session-context span processor")

    # Surface chrys-emitted info logs through the OTel LoggingHandler that
    # _configure_providers installed on the root logger.  Without this, chrys
    # loggers inherit the root default (WARNING) and info-level events never
    # reach the OTel logs sink.  We raise *only* the ``chrys`` tree so
    # third-party library loggers stay at their configured level — and only
    # on success, so failure paths leave the logging tree untouched.  The
    # raise is a *minimum-verbosity* guarantee: a pre-existing more-verbose
    # pin (the TUI log viewer escalates ``chrys`` to DEBUG) already admits
    # INFO records and must not be clamped down to INFO.  The prior level is
    # tracked (only when actually changed) so a later re-run can restore it.
    global _PRIOR_CHRYS_LOGGER_LEVEL
    chrys_logger = logging.getLogger("chrys")
    if chrys_logger.level == logging.NOTSET or chrys_logger.level > logging.INFO:
        _PRIOR_CHRYS_LOGGER_LEVEL = chrys_logger.level
        chrys_logger.setLevel(logging.INFO)

    # Open the chrys telemetry gate: the kernel chat/agent telemetry layers
    # gate on this, as do the owned MCP client spans.
    configure_telemetry(enabled=True, sensitive_data=settings.otel_sensitive_data)

    set_otel_sink(sink)

    if use_file_fallback:
        logger.info(
            "OpenTelemetry enabled: file exporter "
            "(spans + logs → {session_dir}/otel/ once a session starts; sensitive_data=%s)",
            settings.otel_sensitive_data,
        )
    else:
        logger.info(
            "OpenTelemetry enabled: OTLP exporter (endpoint=%s, protocol=%s, sensitive_data=%s)",
            _base_endpoint(settings.otel_endpoint) or "<signal-specific>",
            os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "<default>"),
            settings.otel_sensitive_data,
        )
    return sink
