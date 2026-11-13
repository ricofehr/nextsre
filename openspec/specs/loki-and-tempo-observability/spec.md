# loki-and-tempo-observability Specification

## Purpose
Provides read-only Loki log search, Grafana Tempo trace search and lookup, log-to-trace correlation via `trace_id`, and safe LogQL/TraceQL construction against untrusted LLM inputs.

## Requirements

### Requirement: Loki log search MCP server and safe LogQL backend
The `loki_logs` read-only MCP server (default port `8103`) SHALL expose `search_logs(query, service, minutes, size)` with clamped `minutes` (`0..1440`) and `size` (`0..200`), returning records with `@timestamp`, `service`, `level`, `message`, and `trace_id`. In real mode (`DEMO_MODE=0`), the logs backend SHALL query Loki's `/loki/api/v1/query_range` at `LOKI_URL` (with optional `LOKI_USERNAME`/`LOKI_PASSWORD` basic auth) using escaped LogQL literals that prevent selector or pipeline injection.

#### Scenario: Demo log search returns correlated trace IDs on incident logs
- **WHEN** `search_logs("error", service="payments-api")` is called in demo mode while `payments-api` is unhealthy
- **THEN** it returns `ERROR`-level log documents for `payments-api` that include a valid 32-hex-character `trace_id`

#### Scenario: Real Loki backend escapes untrusted query and service inputs and extracts trace_id
- **WHEN** `RealLogs.search` runs against a Loki `/loki/api/v1/query_range` response with a `service` or `query` containing quotes, backslashes, or LogQL operators (e.g. `payments" |= "secret`)
- **THEN** the constructed LogQL escapes the characters inside string literals without breaking out into new label matchers or pipeline stages, sends basic auth when `LOKI_USERNAME` and `LOKI_PASSWORD` are configured, and returns parsed log records with extracted `trace_id` values

#### Scenario: Explicit zero size or minutes returns no logs
- **WHEN** `search_logs` is called with `size=0`
- **THEN** it returns an empty list `[]` without returning all entries

### Requirement: Tempo distributed trace search and lookup MCP server
The `tempo_traces` read-only MCP server (default port `8104`) SHALL expose `search_traces(service, min_duration_ms, status, minutes, limit)` (clamping `minutes` to `0..1440` and `limit` to `0..100`) and `get_trace(trace_id)` (validating `trace_id` as a 16- or 32-hex string). Demo mode SHALL synthesize traces correlated with `DemoLogs` `trace_id` values, and real mode SHALL query Tempo's `/api/search` (with escaped TraceQL literals) and `/api/traces/<trace_id>` at `TEMPO_URL`.

#### Scenario: Demo trace search and lookup correlate with incident logs
- **WHEN** `search_traces(service="payments-api", status="error")` is called in demo mode during an active incident, followed by `get_trace(trace_id)` using the returned `trace_id` (which matches the `trace_id` in `search_logs`)
- **THEN** `search_traces` returns the failing trace summary for `payments-api` and `get_trace` returns the span list showing the root error reason, and after `restart_deployment("payments-api", "payments")` error trace searches for `payments-api` return no active error traces

#### Scenario: Invalid trace_id is rejected before reaching Tempo
- **WHEN** `get_trace` is called with a non-hexadecimal or path-traversal string such as `"../../api/echo"` or `"not-a-trace-id"`
- **THEN** it raises `ValueError` before making any backend or HTTP request

#### Scenario: Real Tempo backend queries search and trace endpoints safely
- **WHEN** `RealTraces.search_traces` and `RealTraces.get_trace` run against a Tempo HTTP endpoint (`TEMPO_URL`)
- **THEN** `search_traces` builds a TraceQL query with escaped string literals over `/api/search` and `get_trace` fetches `/api/traces/<trace_id>` and normalizes its batch/span structure into a summary dictionary
