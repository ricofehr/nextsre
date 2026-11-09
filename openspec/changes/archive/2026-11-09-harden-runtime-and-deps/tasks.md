# Tasks

## 1. Shell & HTTP Security Hardening

- [x] 1.1 Update `mcp_servers/shell_exec.py` (`DENIED_TOKENS`, `ALLOWED_FLAGS`, `_fragments`, `_is_short_flag`, `_denied`) to extract attached short flags, match short flags case-sensitively, deny `--root`/`-D`/`--directory`/`--file`, and allowlist flags for `df`, `free`, `dig`, and `uptime`; verify with `pytest tests/test_harness.py`.
- [x] 1.2 Add `allowed_hosts(service)` in `common/serving.py` and wrap Starlette A2A apps in `TrustedHostMiddleware` in `agents/a2a_app.py`, passing `service="observer"` in `agents/observer/server.py` and `service="operator"` in `agents/operator/server.py`; verify DNS-rebinding rejection in `pytest tests/test_incident_flow.py`.
- [x] 1.3 Add regression test cases in `tests/test_harness.py` and `tests/test_incident_flow.py` and update security documentation in `README.md` and `DEEP.md`; verify `pytest tests/test_harness.py tests/test_incident_flow.py` passes.

## 2. Agent Runtime Lifecycle & Concurrency

- [x] 2.1 Implement `build_react_agent()` in `common/llm.py` over `StateGraph(MessagesState)`, `ToolNode`, and `tools_condition`, and switch `agents/observer/observer_agent.py` and `agents/operator/operator_agent.py` to use it; verify agent graph tests pass via `pytest`.
- [x] 2.2 Guard `LangGraphExecutor._ensure()` in `agents/a2a_app.py` with an `asyncio.Lock` and add `test_executor_ensure_builds_agent_once_under_concurrency` in `tests/test_incident_flow.py`; verify with `pytest tests/test_incident_flow.py`.
- [x] 2.3 Resolve MCP and A2A URLs at call time in `agents/observer/observer_agent.py`, `agents/operator/operator_agent.py`, `agents/observer/remediation_tool.py`, and `scripts/fire_incident.py`, and add `test_agent_urls_are_resolved_at_call_time` in `tests/test_privilege_boundary.py`; verify with `pytest tests/test_privilege_boundary.py`.
- [x] 2.4 Add cross-process `fcntl.flock` locking (`_locked_state_path`) and temp-file error cleanup in `mcp_servers/demo_state.py`, and wrap Kubernetes `ApiClient` and `Elasticsearch` clients in context managers in `mcp_servers/backends/real.py`; verify with `pytest` and `python -m tests.smoke_test`.

## 3. Dependencies & Container Hardening

- [x] 3.1 Update `pyproject.toml` to require Python `>=3.12` and bump pinned runtime/optional/dev dependencies (`a2a-sdk[http-server]==1.3.0`, `mcp==1.30.0`, `langgraph==1.2.20`, `langchain-openai==1.7.4`, `langchain-core==1.7.6`, `uvicorn[standard]==0.54.0`, `starlette==1.10.0`); verify with `pytest` and `flake8`.
- [x] 3.2 Harden `docker-compose.yml` (`read_only: true`, `/tmp` tmpfs, `no-new-privileges:true`), `Dockerfile`, and `.dockerignore`, and document the container and dependency updates in `README.md` and `DEEP.md`; verify `git diff` and full test suite `pytest`.

## Workflow follow-up

- Archive the change after the PR merges (`openspec-archive.yml` CI workflow or `mr new chore/archive-harden-runtime-and-deps --prompt "/opsx:archive harden-runtime-and-deps"`).
