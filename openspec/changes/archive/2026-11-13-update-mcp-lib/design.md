# Design

## Context

NextSRE runs five standalone HTTP MCP servers (`k8s_readonly`, `prometheus_alerts`, `es_logs`, `k8s_write`, `shell_exec`). In `mcp` 1.x, these modules instantiated `FastMCP(name, host=..., port=..., transport_security=...)` and called `mcp.run(transport="streamable-http")`.

In `mcp` 2.x, `FastMCP` was renamed to `MCPServer` (located at `mcp.server.mcpserver`). Network binding parameters (`host`, `port`, `transport_security`, `streamable_http_path`) are no longer accepted on `MCPServer.__init__`; instead, they are passed as kwargs to `run(transport="streamable-http", ...)`. The agent runtime (`google-adk[mcp]==2.14.0`) is already forward-compatible with `mcp` 2.x via its internal dependency bridge.

## Goals / Non-Goals

**Goals:**
- Pin `mcp==2.3.0` in `pyproject.toml`.
- Migrate server definitions across `mcp_servers/` from `FastMCP` to `MCPServer`.
- Pass `host`, `port`, and `transport_security` to `mcp.run(...)` under `if __name__ == "__main__":`.
- Preserve exact tool signatures, return types, error handling, and direct function callability for existing tests.
- Maintain identical endpoint routing (`/mcp`), port allocations (8101-8103, 8201-8202), and DNS-rebinding security checks.
- Keep documentation and references coherent with `mcp` 2.x.

**Non-Goals:**
- Changing tool functionality, harness policies, or data formats.
- Changing agent architectures, A2A communication, or Starlette A2A apps.
- Introducing new external transports (e.g. SSE or stdio) beyond the established streamable-HTTP setup.

## Decisions

### 1. Upgrade pin to `mcp==2.3.0`
- **Choice**: Pin `mcp==2.3.0` in `pyproject.toml`.
- **Rationale**: `2.3.0` is the latest stable release in the 2.x line. It satisfies `google-adk[mcp]`'s `mcp<3,>=1.24` dependency range.
- **Alternatives considered**: Floating `mcp>=2.0,<3` — rejected because NextSRE pins exact dependencies for reproducible deployments.

### 2. Constructor vs `run()` argument separation in `mcp_servers/`
- **Choice**:
  ```python
  from mcp.server.mcpserver import MCPServer

  mcp = MCPServer("k8s-readonly")
  ...
  if __name__ == "__main__":
      env_flag("DEMO_MODE", default=True)
      mcp.run(
          transport="streamable-http",
          host=bind_host(),
          port=port(default=8101),
          transport_security=transport_security("k8s-ro"),
      )
  ```
- **Rationale**: In `mcp` 2.x, `MCPServer.__init__` handles server metadata, tools, resources, and prompts, while transport options belong to `run()`. Default `streamable_http_path` in `run_streamable_http_async` remains `"/mcp"`.
- **Alternatives considered**: Subclassing `MCPServer` or wrapping in custom ASGI factory — rejected as unnecessary complexity when `mcp.run(...)` directly handles the serving lifecycle.

### 3. Maintain `TransportSecuritySettings`
- **Choice**: Continue importing `TransportSecuritySettings` from `mcp.server.transport_security` via `common/serving.py`.
- **Rationale**: The class remains unchanged in 2.x and is passed directly to the streamable HTTP session manager.

## Risks / Trade-offs

- **[Risk] Test breakage from decorator behavior** → **Mitigation**: `MCPServer.tool()` returns the decorated callable directly, ensuring tests importing functions like `list_pods` or `restart_deployment` execute without changes.
- **[Risk] Starlette or transport security dependency conflicts** → **Mitigation**: `mcp==2.3.0` requires `starlette>=0.27` (or `>=0.48.0` on Python 3.14+), which is satisfied by project's `starlette==1.10.0`.
