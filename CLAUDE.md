# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NextSRE is a distributed SRE incident-response system that splits investigation from remediation across a hard privilege boundary. An **Observer** agent (read-only) investigates incidents and delegates write actions to an **Operator** agent over the A2A protocol. All tools are served by independent MCP servers.

## Commands

### Setup
```bash
cp .env.example .env   # configure LLM endpoint and optional harness knobs
```

### Run (Docker)
```bash
docker compose up --build
# Then trigger a synthetic incident:
python -m scripts.fire_incident
```

### Run (without Docker)
Start each service in a separate terminal — 6 MCP servers on ports 8101–8104 and 8201–8202, then Observer on 9300 and Operator on 9301. See `docker-compose.yml` for the exact commands and env vars.

### Test
```bash
pip install -e ".[dev]"
pytest                        # full suite; offline, no LLM or cluster required
python -m tests.smoke_test    # dependency-free subset
```

`tests/test_harness.py` holds the policy-harness regressions — every case there is a
command or config that the harness once accepted. `tests/test_incident_flow.py` covers
read tools, the recovery loop and the A2A round-trip. `tests/smoke_test.py` is a
pytest-free demo-flow check only; it deliberately does not assert the harness, so a
green smoke run says nothing about the security boundary.

### Lint / Format
```bash
black . && isort . && flake8
```
Configured in `pyproject.toml` (`[tool.black]`, `[tool.isort]`) and `.flake8`; line
length 100.

## Architecture

### Agent Split
- **Observer** (`agents/observer/`) — read-only tools only; cannot mutate infrastructure directly. When remediation is required it calls `request_remediation` which sends an A2A task to the Operator.
- **Operator** (`agents/operator/`) — write tools with a three-layer policy harness. It re-evaluates the Observer's proposal against its own harness rather than blindly executing it.
- **`agents/a2a_app.py`** — shared infrastructure: Starlette app builder, `ADKExecutor` (bridges ADK's `A2aAgentExecutor` and `InMemoryRunner` with single-flight lazy initialization and sanitized failure messages), and `call_agent` A2A client helper.

### MCP Servers (`mcp_servers/`)
Six independent HTTP MCP servers provide all tools. Agents load tools via ADK `McpToolset` (`StreamableHTTPConnectionParams`).

| Server | Port | Access |
|---|---|---|
| `k8s_readonly.py` | 8101 | Observer |
| `prometheus_alerts.py` | 8102 | Observer |
| `loki_logs.py` | 8103 | Observer |
| `tempo_traces.py` | 8104 | Observer |
| `k8s_write.py` | 8201 | Operator |
| `shell_exec.py` | 8202 | Operator |

`demo_state.py` is a shared module (not a server) providing thread-safe, atomic-write JSON state used by all servers in demo mode.

### OpenTelemetry Self-Instrumentation (`common/telemetry.py`)
Setting `OTEL_EXPORTER_OTLP_ENDPOINT` (and optional `OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_SERVICE_NAME`) enables OTLP span export across both A2A agents and all MCP servers, with W3C `traceparent` propagation across `call_agent` → `build_app` and outbound backend HTTP requests. Leaving `OTEL_EXPORTER_OTLP_ENDPOINT` unset is a strict no-op.

### Defense-in-Depth Harness (Operator)
Three independent layers prevent unsafe mutations:
1. **MCP tool hard policy** — `k8s_write.py` enforces `ALLOWED_NAMESPACES` and `MAX_REPLICAS` env vars; `shell_exec.py` checks binary allowlist, substring denylist, and per-binary verb whitelist. Both raise before any mutation.
2. **Behavioral policy** — system prompt on the Operator.
3. **Human approval gate** — ADK tool confirmation (`require_confirmation=True`), activated only when `REQUIRE_APPROVAL=1`.

### LLM Integration
Both agents use Google ADK's `OpenAILlm` (`google.adk.integrations.openai`) configured as an OpenAI-compatible client (`common/llm.py`) pointing to a local Ollama or llama-server instance — not a cloud API. Set `LLM_BASE_URL` and `LLM_MODEL` in `.env`.

### Demo Mode
`DEMO_MODE=1` enables a fully synthetic incident: `demo_state.py` maintains fake cluster state in a shared JSON file (path set by `DEMO_STATE_FILE`). All six MCP servers read/write this file so the incident progresses realistically without real infrastructure.

### A2A Protocol
Uses A2A SDK 1.x (proto-based). The `fire_incident.py` script sends a JSON-RPC task to the Observer and streams status updates until the incident is resolved. The pinned dependency versions in `pyproject.toml` are load-bearing — A2A 1.x and 0.x are not compatible.
