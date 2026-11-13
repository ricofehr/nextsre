# Proposal

## Why

The target Kubernetes cluster observability stack has replaced Elasticsearch/Kibana (ELK) with Grafana Loki (behind a basic-auth gateway) and added an OpenTelemetry tracing pipeline (OpenTelemetry Collector gateway + Grafana Tempo, with log-to-trace `trace_id` correlation and span metrics). NextSRE still queries Elasticsearch via `es_logs`, lacks any tool to inspect distributed traces in Tempo during incident investigation, and does not emit or propagate OpenTelemetry traces across its own A2A and MCP hops.

## What Changes

- **BREAKING**: Replace `mcp_servers/es_logs.py` (`es-logs`, `ES_LOGS_URL`, `ES_URL`) with `mcp_servers/loki_logs.py` (`loki-logs`, `LOKI_LOGS_URL`, `LOKI_URL`, plus optional `LOKI_USERNAME` and `LOKI_PASSWORD` basic auth) and drop `elasticsearch` from `pyproject.toml`'s `[project.optional-dependencies].real`.
- Implement `RealLogs` against Loki's `/loki/api/v1/query_range` HTTP API using `httpx` with strict LogQL label/filter escaping so LLM-supplied `service` and `query` strings cannot inject LogQL selectors or pipeline operators, while surfacing `trace_id` metadata on returned log records (in both `RealLogs` and `DemoLogs`).
- Add a fourth read-only MCP server, `mcp_servers/tempo_traces.py` (`tempo-traces` on port `8104`, `TEMPO_TRACES_URL`, `TEMPO_URL`), exposing `search_traces` and `get_trace` over `TracesPort` (`DemoTraces` and `RealTraces` against Tempo's `/api/search` and `/api/traces/<trace_id>` HTTP endpoints) and wire it into the Observer agent (`agents/observer/observer_agent.py`, `agents/observer/card.py`, `docker-compose.yml`).
- Add opt-in OpenTelemetry tracing (`common/telemetry.py`) across both A2A agents and all MCP servers: when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, configure an `opentelemetry-sdk` `TracerProvider` exporting OTLP spans (`grpc` or `http/protobuf`), trace A2A task execution and client calls with W3C `traceparent` propagation, and trace MCP tool invocations; when unset, stay a zero-network no-op so offline tests remain deterministic.
- Update unit tests (`tests/test_incident_flow.py`, `tests/test_privilege_boundary.py`, `tests/test_harness.py`, `tests/smoke_test.py`) and documentation (`README.md`, `CLAUDE.md`, `DEEP.md`, `.env.example`) for the Loki + Tempo + OpenTelemetry stack.

## Capabilities

### New Capabilities
- `loki-and-tempo-observability`: Read-only Loki log querying (`loki_logs`), Tempo trace search and lookup (`tempo_traces`), log-to-trace `trace_id` correlation in demo and real backends, and LogQL/TraceQL input sanitization.
- `agent-opentelemetry-tracing`: Opt-in OpenTelemetry SDK configuration, OTLP span export, W3C `traceparent` context propagation across Observer -> A2A -> Operator and MCP tool calls, and safe attribute recording without leaking prompt/secret content.

### Modified Capabilities
- `agent-runtime-lifecycle`: Update call-time URL resolution and Observer read-toolset wiring to replace `ES_LOGS_URL` (`es_logs`) with `LOKI_LOGS_URL` (`loki_logs`) and add `TEMPO_TRACES_URL` (`tempo_traces`), and remove the obsolete Elasticsearch client requirement.

## Impact

- **Branch**: `observe0otel-instead-elk`
- **Dependencies**: `pyproject.toml` removes `elasticsearch>=8.19,<10` from `[project.optional-dependencies].real` and adds pinned `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`, and `opentelemetry-exporter-otlp-proto-http` to `dependencies`.
- **Code**: `common/telemetry.py`, `mcp_servers/loki_logs.py` (replaces `mcp_servers/es_logs.py`), `mcp_servers/tempo_traces.py`, `mcp_servers/backends/ports.py`, `mcp_servers/backends/demo.py`, `mcp_servers/backends/real.py`, `mcp_servers/backends/__init__.py`, `mcp_servers/demo_state.py`, `agents/a2a_app.py`, `agents/observer/observer_agent.py`, `agents/observer/card.py`, `agents/observer/server.py`, `agents/operator/server.py`, `docker-compose.yml`, `.env.example`.
- **Tests & Docs**: `tests/test_incident_flow.py`, `tests/test_privilege_boundary.py`, `tests/test_harness.py`, `tests/smoke_test.py`, `README.md`, `CLAUDE.md`, `DEEP.md`.
