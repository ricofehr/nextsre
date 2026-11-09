# Proposal

## Why

The Operator's shell policy harness and the A2A agent HTTP servers had several defense-in-depth gaps (attached short flags like `-fhttps://...` or `-Huser@remote`, verb-less binaries without flag allowlists, and missing `Host`-header validation on A2A servers), while runtime components had cold-start race conditions, import-frozen URLs, unclosed backend clients, deprecated `langgraph.prebuilt.create_react_agent` calls, and outdated dependency pins with known CVEs. This change hardens the security boundaries, fixes concurrency and resource-lifecycle behavior, and updates the runtime and container configuration to Python 3.12+ and current safe dependency versions.

## What Changes

- **Shell execution harness hardening (`mcp_servers/shell_exec.py`)**:
  - Extract attached short flags (e.g., `-fhttps://...`, `-Huser@remote`, `-Mother`) in `_fragments()` so attached values cannot bypass `DENIED_TOKENS`.
  - Match short flags case-sensitively in `_denied()` so `-H`/`-M`/`-D` are blocked without rejecting `-h`/`-m`/`-d`.
  - Add `--root`, `-D`, `--directory`, and `--file` to `DENIED_TOKENS`.
  - Add explicit `ALLOWED_FLAGS` allowlists for verb-less binaries `df`, `free`, `dig`, and `uptime`.
- **A2A HTTP DNS-rebinding protection (`common/serving.py`, `agents/a2a_app.py`, `agents/observer/server.py`, `agents/operator/server.py`)**:
  - Extract `allowed_hosts(service)` in `common/serving.py` and wrap Starlette A2A apps in `TrustedHostMiddleware` permitting configured hosts plus hostnames declared in the agent card's `supported_interfaces`.
- **Agent runtime lifecycle & concurrency (`agents/a2a_app.py`, `common/llm.py`, `agents/observer/`, `agents/operator/`, `scripts/fire_incident.py`, `mcp_servers/demo_state.py`, `mcp_servers/backends/real.py`)**:
  - Guard `LangGraphExecutor._ensure()` with an `asyncio.Lock` to prevent duplicate agent builds under concurrent cold-start requests.
  - Replace deprecated `langgraph.prebuilt.create_react_agent` with `build_react_agent()` in `common/llm.py` (`StateGraph(MessagesState)` over `ToolNode` and `tools_condition`).
  - Resolve MCP server URLs, `OPERATOR_URL`, and `OBSERVER_URL` at call time rather than freezing them at module import time.
  - Add cross-process advisory file locking (`fcntl.flock` on `<state>.lock`) and temp-file cleanup on write errors in `mcp_servers/demo_state.py`.
  - Manage `kubernetes.client.ApiClient` and `elasticsearch.Elasticsearch` lifecycles with context managers in `mcp_servers/backends/real.py`.
- **Container & dependency updates (`pyproject.toml`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`)**:
  - **BREAKING**: Require Python `>=3.12` in `pyproject.toml`.
  - Upgrade pinned dependencies (`a2a-sdk[http-server]==1.3.0`, `mcp==1.30.0`, `langgraph==1.2.20`, `langchain-openai==1.7.4`, `langchain-core==1.7.6`, `uvicorn[standard]==0.54.0`, `starlette==1.10.0`) and `real`/`dev` optional dependency ranges.
  - Harden all 7 Docker Compose services with `read_only: true`, `/tmp` on `tmpfs`, and `no-new-privileges:true`, and exclude tests/plans/cache from the Docker build context.

## Capabilities

### New Capabilities
- `shell-and-http-hardening`: Shell command token/flag validation in `shell_exec.py` and HTTP `Host` header DNS-rebinding protection across MCP and A2A servers.
- `agent-runtime-lifecycle`: ReAct graph construction, cold-start concurrency safety, call-time endpoint resolution, cross-process demo state locking, and backend client resource management.

### Modified Capabilities

## Impact

- Branch: `feat/harden-runtime-and-deps`
- Affected code: `mcp_servers/shell_exec.py`, `mcp_servers/demo_state.py`, `mcp_servers/backends/real.py`, `common/llm.py`, `common/serving.py`, `agents/a2a_app.py`, `agents/observer/*`, `agents/operator/*`, `scripts/fire_incident.py`
- Affected infrastructure & packaging: `pyproject.toml`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`
- Affected tests & docs: `tests/test_harness.py`, `tests/test_incident_flow.py`, `tests/test_privilege_boundary.py`, `README.md`, `DEEP.md`
