# Proposal

## Why

The `mcp` dependency is currently pinned to `1.30.0` because earlier LangChain MCP adapters (`langchain-mcp-adapters`) required `mcp<2.0.0`. With the runtime migrated to `google-adk` (which supports `mcp<3,>=1.24`), NextSRE can safely upgrade to `mcp==2.3.0` (the latest 2.x release), keeping tool servers aligned with current MCP SDK standards, modern streamable HTTP server lifecycles, and upstream protocol improvements.

## What Changes

- Bump `mcp` dependency from `1.30.0` to `2.3.0` in `pyproject.toml`.
- Migrate all five MCP tool servers (`k8s_readonly`, `prometheus_alerts`, `es_logs`, `k8s_write`, `shell_exec`) from `FastMCP` (`mcp.server.fastmcp`) to `MCPServer` (`mcp.server.mcpserver`).
- Update server invocation: instantiate `MCPServer("<name>")` and pass `host`, `port`, and `transport_security` to `mcp.run(transport="streamable-http", host=..., port=..., transport_security=...)`.
- Update project documentation (`README.md`, `CLAUDE.md`, `DEEP.md`) and code comments/docstrings in `common/serving.py` to reflect the 2.x SDK and `MCPServer`.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
None (library upgrade and internal server refactor preserving existing external interfaces, tools, and security boundaries; `skip_specs: true`).

## Impact

- **Dependencies**: `pyproject.toml` pins `mcp==2.3.0`.
- **Code**: `mcp_servers/k8s_readonly.py`, `mcp_servers/prometheus_alerts.py`, `mcp_servers/es_logs.py`, `mcp_servers/k8s_write.py`, `mcp_servers/shell_exec.py`, `common/serving.py`.
- **Docs**: `README.md`, `CLAUDE.md`, `DEEP.md`.
- **Tests**: Suite continues to pass offline (`pytest`, `tests.smoke_test`).
