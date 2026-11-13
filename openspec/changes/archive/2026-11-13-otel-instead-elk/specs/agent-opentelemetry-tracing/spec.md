# Spec Delta

## Purpose

Configures opt-in OpenTelemetry distributed tracing across the Observer agent, Operator agent, and MCP tool servers, propagating W3C trace context across A2A and MCP boundaries and exporting spans over OTLP without leaking sensitive prompt or secret data.

## ADDED Requirements

### Requirement: Opt-in OpenTelemetry OTLP exporter initialization
Each agent server and MCP server SHALL support OpenTelemetry tracing configured via `common/telemetry.py`. When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset or empty, tracing setup SHALL remain a local no-op with zero network connections. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the runtime SHALL configure a `TracerProvider` with `service.name` (overridable by `OTEL_SERVICE_NAME`) and an OTLP span exporter (`grpc` or `http/protobuf`).

#### Scenario: Unset endpoint leaves tracing as a network-free no-op
- **WHEN** `init_telemetry("observer")` is called with `OTEL_EXPORTER_OTLP_ENDPOINT` unset
- **THEN** it returns `False` and registers no network OTLP span processor

#### Scenario: Configured endpoint registers OTLP span export
- **WHEN** `init_telemetry("observer")` is called with `OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector.kube-tracing.svc.cluster.local:4317"`
- **THEN** it configures a `TracerProvider` with resource `service.name="observer"` and an OTLP exporter targeting that endpoint

### Requirement: W3C traceparent propagation and span recording across A2A and MCP calls
The A2A client (`call_agent`) SHALL create a client span and inject the current W3C `traceparent` (and `tracestate`) headers into outgoing HTTP requests, and the A2A ASGI server (`build_app`) SHALL extract incoming W3C trace context and run request handling inside a server span. MCP tool invocations SHALL record spans carrying the tool name and status without recording raw prompt text, model completions, or secret material in span attributes.

#### Scenario: A2A round-trip propagates W3C trace context from client to server
- **WHEN** `call_agent` sends a request while an active OpenTelemetry span is current
- **THEN** the outgoing HTTP request carries a valid `traceparent` header with the caller's `trace_id`, and the A2A server extracts that context so its server span shares the same `trace_id`
