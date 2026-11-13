# Tasks

## 1. Dependencies & OpenTelemetry Instrumentation Module

- [x] 1.1 Remove `elasticsearch>=8.19,<10` from `[project.optional-dependencies].real` in `pyproject.toml`, add pinned `opentelemetry-api==1.45.2`, `opentelemetry-sdk==1.45.2`, `opentelemetry-exporter-otlp-proto-grpc==1.45.2`, and `opentelemetry-exporter-otlp-proto-http==1.45.2` to `dependencies`, and verify installation and `pip-audit` succeed
- [x] 1.2 Create `common/telemetry.py` with `init_telemetry(default_service_name)` (no-op when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, configuring `TracerProvider` + `grpc` or `http/protobuf` OTLP span exporter when set), `get_tracer(name)`, and `traced_tool` helper, and verify unit tests for unset vs configured endpoints pass
- [x] 1.3 Wire W3C `traceparent` injection into `call_agent` and extraction + server span creation into `build_app` in `agents/a2a_app.py`, call `init_telemetry` in `agents/observer/server.py`, `agents/operator/server.py`, and the MCP server entrypoints, and verify a unit test in `tests/test_incident_flow.py` confirms `traceparent` propagation across an A2A round-trip

## 2. Loki Logs Backend & MCP Server (`loki_logs`)

- [x] 2.1 Replace `mcp_servers/es_logs.py` with `mcp_servers/loki_logs.py` (`MCPServer("loki-logs")`, default port `8103`, `transport_security("loki-logs")`), update `DemoLogs` in `mcp_servers/backends/demo.py` to include correlated 32-hex `trace_id` fields on log documents, and implement `RealLogs` in `mcp_servers/backends/real.py` querying Loki's `/loki/api/v1/query_range` via `httpx` with escaped LogQL selectors/filters and optional `LOKI_USERNAME`/`LOKI_PASSWORD` basic auth; verify with unit tests in `tests/test_incident_flow.py` and `tests/test_harness.py` (including LogQL injection escaping)

## 3. Tempo Traces Backend, MCP Server (`tempo_traces`), & Observer Wiring

- [x] 3.1 Add `TracesPort` to `mcp_servers/backends/ports.py`, `DemoTraces` to `mcp_servers/backends/demo.py`, `RealTraces` to `mcp_servers/backends/real.py` (querying Tempo's `/api/search` with escaped TraceQL and `/api/traces/<trace_id>`), and `traces()` to `mcp_servers/backends/__init__.py`, and create `mcp_servers/tempo_traces.py` (`MCPServer("tempo-traces")`, default port `8104`, `search_traces` and `get_trace` with `trace_id` hex validation); verify with unit tests in `tests/test_incident_flow.py` and `tests/test_harness.py` (including invalid `trace_id` rejection and demo recovery correlation)
- [x] 3.2 Update `agents/observer/observer_agent.py` and `agents/observer/card.py` to replace `es_logs` (`ES_LOGS_URL`) with `loki_logs` (`LOKI_LOGS_URL`) and add `tempo_traces` (`TEMPO_TRACES_URL`, default `http://localhost:8104/mcp`), updating the Observer system prompt to correlate Loki logs and Tempo traces via `trace_id`; verify `tests/test_privilege_boundary.py` and `tests/smoke_test.py` pass

## 4. Compose, Environment, & Documentation

- [x] 4.1 Update `docker-compose.yml`, `.env.example`, `README.md`, `CLAUDE.md`, and `DEEP.md` to replace `es-logs` / `ES_LOGS_URL` / `ES_URL` with `loki-logs` / `LOKI_LOGS_URL` / `LOKI_URL` (`LOKI_USERNAME`, `LOKI_PASSWORD`), add the `tempo-traces` service on port `8104` (`TEMPO_TRACES_URL`, `TEMPO_URL`), and document `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_SERVICE_NAME`; verify `pytest`, `python -m tests.smoke_test`, `black --check .`, `isort --check-only .`, and `flake8` all pass

## Workflow follow-up

- Archive the change after the PR merges (`openspec-archive.yml` or manual archive MR).
