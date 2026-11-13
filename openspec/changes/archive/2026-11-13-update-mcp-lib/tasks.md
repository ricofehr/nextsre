# Tasks

## 1. Dependency Bump and Serving Configuration

- [x] 1.1 Update `pyproject.toml` to pin `mcp==2.3.0` and update explanatory comments
- [x] 1.2 Update `common/serving.py` docstrings and references to reflect `MCPServer`

## 2. MCP Servers Migration

- [x] 2.1 Migrate read-only MCP servers (`mcp_servers/k8s_readonly.py`, `mcp_servers/prometheus_alerts.py`, `mcp_servers/es_logs.py`) to import `MCPServer` from `mcp.server.mcpserver` and pass host, port, and transport security in `mcp.run(...)`
- [x] 2.2 Migrate write MCP servers (`mcp_servers/k8s_write.py`, `mcp_servers/shell_exec.py`) to import `MCPServer` from `mcp.server.mcpserver` and pass host, port, and transport security in `mcp.run(...)`

## 3. Documentation and Verification

- [x] 3.1 Update `README.md`, `CLAUDE.md`, and `DEEP.md` references to `FastMCP` and `mcp` 1.x
- [x] 3.2 Run test suite (`pytest`, `tests.smoke_test`) and linters (`black`, `isort`, `flake8`) to verify clean migration
