# Design

## Context

- See `proposal.md` for motivation and scope.
- On the target Kubernetes cluster, the observability stack consists of:
  - **Loki** (`loki-gateway.kube-logcollect.svc.cluster.local`, port 80) behind nginx basic auth (`loki-basic-auth` secret: `username` and `password`), receiving container logs from Fluent Bit (`job="fluentbit"`, `namespace_name`, `pod_name`, `container_name`) and OTLP logs from the OpenTelemetry Collector (`service_name`, `k8s.namespace.name`, `k8s.pod.name`), with `trace_id` embedded in JSON/text log lines or structured metadata.
  - **Tempo** (`tempo.kube-tracing.svc.cluster.local:3200` for HTTP queries), receiving spans from the OpenTelemetry Collector and linking traces to Loki logs (`k8s.namespace.name` -> `namespace_name`, `k8s.pod.name` -> `pod_name`, `trace_id`) and Prometheus span metrics (`traces_spanmetrics_*`).
  - **OpenTelemetry Collector gateway** (`otel-collector.kube-tracing.svc.cluster.local:4317` gRPC / `:4318` HTTP, plaintext in-cluster), enriching signals with `k8s.*` resource attributes by source pod IP.
- NextSRE already includes `httpx==0.28.1` and `google-adk==2.14.0` (which depends on `opentelemetry-api` and `opentelemetry-sdk`).

## Goals / Non-Goals

**Goals:**
- Replace Elasticsearch with Loki using `httpx` so no extra client library is needed in `[project.optional-dependencies].real`.
- Prevent LogQL and TraceQL injection from untrusted LLM tool arguments (`service`, `query`, `status`, `trace_id`).
- Keep demo mode (`DEMO_MODE=1`) and the offline `pytest` / `smoke_test` suites completely self-contained and deterministic, with correlated `trace_id` values across demo logs and demo traces.
- Make OpenTelemetry self-instrumentation opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`, propagating W3C `traceparent` headers across Observer -> A2A -> Operator without recording sensitive prompt/completion or secret material on spans.

**Non-Goals:**
- Modifying the Operator's write tools (`k8s_write`, `shell_exec`) or weakening any policy harness rule.
- Running a collector or Tempo container inside `docker-compose.yml` (demo mode serves synthetic traces via `tempo-traces`, and OTLP export targets an external collector when configured).

## Decisions

### D1. Loki query construction and basic auth (`mcp_servers/loki_logs.py` & `RealLogs`)
`RealLogs.search(query, service, minutes, size)` queries `GET {LOKI_URL}/loki/api/v1/query_range` using `httpx`:
- **Stream selector**:
  - When `service` is provided, escape `\` and `"` and match against common workload labels emitted by Fluent Bit and OTLP in the cluster (`{pod_name=~"<escaped>.*", container_name!="" }` or a regex across `service_name|container_name|pod_name|app` using `{job=~".+"}` + label filter `| service_name="<escaped>" or container_name="<escaped>" or pod_name=~"^<escaped_re>($|-.*)"`). Specifically, Loki requires at least one non-empty label matcher in `{...}`, so we use `{job=~".+"}` as the base stream selector (matching `job="fluentbit"`, `job="systemd"`, and OTLP streams), then filter by service and line content.
- **Line filter**:
  - `""` and `"*"` match all lines (no `|~` filter).
  - Otherwise, escape `\` and `"` (and regex metacharacters via `re.escape` inside a case-insensitive `(?i)` regex `|~ "(?i)<escaped>"`) so an LLM-supplied query cannot inject `|`, `"`, or LogQL parser/filter stages.
- **Auth**: If `LOKI_USERNAME` and `LOKI_PASSWORD` are set in the environment, pass `auth=(user, password)` to `httpx.get`.
- **Record shape**: Each returned log entry is normalized to `{"@timestamp": iso_ts, "service": service_val, "level": level_val, "message": message_val, "trace_id": trace_id_val}` so the Observer gets a consistent schema in both demo and real modes. `trace_id` is extracted from stream/structured metadata labels (`trace_id` / `traceID`) or matched from the log line via `(?:trace_?[iI][dD])"?[:=]"?([0-9a-fA-F]{32})`.

*Alternative:* Keep `es_logs.py` as a compatibility wrapper. Rejected per user decision to replace ELK cleanly across code, compose, and docs.

### D2. Dedicated `tempo_traces.py` MCP server on port 8104 (`TracesPort`, `DemoTraces`, `RealTraces`)
A fourth read-only MCP server `mcp_servers/tempo_traces.py` (`MCPServer("tempo-traces")`, default port `8104`, `transport_security("tempo-traces")`) exposes two tools:
1. `search_traces(service: str | None = None, min_duration_ms: int = 0, status: str | None = None, minutes: int = 15, limit: int = 20) -> list[dict]`
2. `get_trace(trace_id: str) -> dict`

- **Input validation**:
  - `minutes` is clamped to `0..1440`, `limit` is clamped to `0..100`, `min_duration_ms` is clamped to `>= 0`.
  - `status` (when provided) must be one of `"error"`, `"ok"`, `"unset"` (case-insensitive); anything else raises `ValueError`.
  - `trace_id` is validated against `^[0-9a-fA-F]{16}([0-9a-fA-F]{16})?$` before calling the backend, preventing path traversal (`../../`) on `/api/traces/{trace_id}`.
- **`DemoTraces`**:
  - Derives deterministic trace summaries and span trees from `demo_state.load()["deployments"]` using a deterministic 32-hex `trace_id` per deployment (shared with `DemoLogs` so the `ERROR` log line for `payments-api` carries the exact `trace_id` that `get_trace(trace_id)` resolves).
  - When `payments-api` is unhealthy, `search_traces(service="payments-api", status="error")` returns the failing trace (`status="error"`, `root_span="POST /checkout"`, error `"OOMKilled: container exceeded memory limit 256Mi"`). Once `restart_deployment` marks `payments-api` healthy, error trace searches return `[]` and general trace searches return healthy spans (`status="ok"`).
- **`RealTraces`**:
  - `search_traces` builds a TraceQL query `{ <conditions> }` (with `\` and `"` escaped in `resource.service.name = "..."`, `status = error|ok|unset`, and `duration >= <N>ms`) and calls `GET {TEMPO_URL}/api/search` with `q`, `start`, `end`, and `limit`.
  - `get_trace` calls `GET {TEMPO_URL}/api/traces/{trace_id}` and normalizes Tempo's OTLP-JSON `batches` into `{"trace_id": ..., "root_service": ..., "status": ..., "spans": [...]}`.

### D3. Opt-in OpenTelemetry instrumentation (`common/telemetry.py`)
- `init_telemetry(default_service_name: str) -> bool`:
  - Reads `OTEL_EXPORTER_OTLP_ENDPOINT` from `os.environ`. If unset/blank, returns `False` without registering an exporter.
  - Validates the endpoint and constructs a `Resource` with `service.name = os.environ.get("OTEL_SERVICE_NAME", default_service_name)`.
  - Selects the OTLP span exporter via `OTEL_EXPORTER_OTLP_PROTOCOL` (`grpc` or `http/protobuf`; defaults to `http/protobuf` if the endpoint ends with `:4318` or `/v1/traces`, else `grpc` for `:4317`, matching `otel-collector.kube-tracing.svc.cluster.local:4317`).
  - Wraps MCP `@mcp.tool()` functions (or provides a `traced_tool` / middleware helper) and A2A `call_agent` / `build_app` with OpenTelemetry spans:
    - `call_agent` creates a `CLIENT` span (`a2a.call_agent`) and calls `opentelemetry.propagate.inject(headers)` on the `httpx.AsyncClient` headers so W3C `traceparent` crosses the A2A boundary.
    - `build_app` adds a lightweight ASGI middleware that extracts incoming W3C `traceparent` via `opentelemetry.propagate.extract(scope_headers)` and opens a `SERVER` span (`a2a.request`).
    - Spans record safe operational metadata only (`rpc.system="a2a"`, `http.route`, `mcp.tool.name`, status code), never raw prompt text, LLM completions, or environment secrets.

## Risks / Trade-offs

- [Log label variations between Fluent Bit and OTLP] → Fluent Bit sets `namespace_name`, `pod_name`, `container_name`, while OTLP logs through the collector set `service_name`, `k8s.namespace.name`, `k8s.pod.name`. `RealLogs` matches `service` across `service_name`, `container_name`, `app`, and `pod_name` prefix so logs from either path are found.
- [Collector unreachable when `OTEL_EXPORTER_OTLP_ENDPOINT` is set] → `BatchSpanProcessor` exports asynchronously in a background thread and drops spans on connection failure without failing agent or tool requests.
- [Extra read server container in `docker-compose.yml`] → Adding `tempo-traces` on port `8104` increases the compose stack from 7 to 8 lightweight containers (sharing the same image and read-only hardening).

## Migration Plan

1. Replace `mcp_servers/es_logs.py` with `mcp_servers/loki_logs.py` and add `mcp_servers/tempo_traces.py`.
2. Update `docker-compose.yml`, `.env.example`, and Observer toolset configuration (`LOKI_LOGS_URL`, `TEMPO_TRACES_URL`).
3. Remove `elasticsearch` from `pyproject.toml` and pin the `opentelemetry-*` packages.
4. Rollback: revert the commit and redeploy `docker compose up --build`.
