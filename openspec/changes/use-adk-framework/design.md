# Design

## Context

See `proposal.md` for motivation. NextSRE runs two A2A agent services (`observer` on port 9300 and `operator` on port 9301) that load tools from five streamable-HTTP MCP servers (`mcp_servers/`) and query a local OpenAI-compatible LLM (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`). We are migrating both agents from `langgraph` + `langchain-*` to `google-adk[a2a,mcp,openai]==2.14.0` while preserving:
- The hard privilege split between Observer (read-only MCP servers + `request_remediation`) and Operator (write MCP servers only).
- DNS-rebinding `Host` header protection (`TrustedHostMiddleware` in `agents/a2a_app.py`).
- Single-flight lazy agent/runner construction and sanitized failure messages on the A2A server boundary.
- Call-time resolution of all MCP and A2A endpoint environment variables.
- The `REQUIRE_APPROVAL=1` human approval gate on Operator tools.

## Goals / Non-Goals

**Goals:**
- Replace all `langchain-*` and `langgraph` imports with `google.adk` equivalents.
- Use `google.adk.integrations.openai.OpenAILlm` for the local OpenAI-compatible inference endpoint.
- Use `google.adk.tools.mcp_tool.McpToolset` with `StreamableHTTPConnectionParams` for MCP tool loading.
- Use `google.adk.a2a.executor.a2a_agent_executor.A2aAgentExecutor` with `InMemoryRunner` for A2A task execution.
- Use ADK's native `require_confirmation=True` on tools/toolsets when `REQUIRE_APPROVAL=1`.

**Non-Goals:**
- Changing the MCP servers (`mcp_servers/*`) or their security harnesses (`k8s_write.py`, `shell_exec.py`).
- Changing the A2A wire protocol or `scripts/fire_incident.py` CLI behavior.

## Decisions

### 1. Local OpenAI-Compatible Model via `OpenAILlm` (`common/llm.py`)
- **Choice**: Construct `OpenAILlm(model=..., base_url=..., api_key=...)` from `google.adk.integrations.openai` with `generate_content_config=types.GenerateContentConfig(temperature=0)` (or equivalent config) and timeout.
- **Rationale**: `OpenAILlm` uses `AsyncOpenAI` directly without pulling in `litellm`, matching the lightweight footprint of the previous client while supporting any OpenAI-compatible local server (Ollama, `llama-server`, vLLM).
- **Agent Builder**: `build_adk_agent(model, tools, *, prompt, name="sre_agent", require_confirmation=False)` returns an `LlmAgent` with `instruction=prompt`, wrapping plain callables as `FunctionTool(..., require_confirmation=require_confirmation)` when `require_confirmation=True`. Provide `build_react_agent = build_adk_agent` as an alias so callers and tests have a clean transition.

### 2. MCP Toolsets (`agents/observer/observer_agent.py`, `agents/operator/operator_agent.py`)
- **Choice**: Replace `_mcp_client() -> MultiServerMCPClient` with `_mcp_toolsets(*, require_confirmation: bool = False) -> dict[str, McpToolset]`, where each entry maps the logical server key (`k8s_ro`, `prometheus_alerts`, `es_logs` for Observer; `k8s_write`, `shell` for Operator) to `McpToolset(connection_params=StreamableHTTPConnectionParams(url=...), require_confirmation=require_confirmation)`.
- **Rationale**: `LlmAgent(tools=[...])` accepts `McpToolset` instances directly and resolves tools lazily during runner execution. Returning a keyed dict from `_mcp_toolsets()` (or exposing a lightweight helper) also makes privilege-boundary inspection in `tests/test_privilege_boundary.py` explicit and deterministic without needing network calls at import or build time.

### 3. Human Approval Gate (`REQUIRE_APPROVAL=1`)
- **Choice**: Pass `require_confirmation=True` to `McpToolset` (and `FunctionTool` where applicable) when `REQUIRE_APPROVAL=1`.
- **Rationale**: ADK's tool execution pipeline checks `tool.check_require_confirmation(...)` in `apply_confirmation_gate`. When unconfirmed, ADK emits an `adk_request_confirmation` long-running function call event (`event.long_running_tool_ids`), which `A2aAgentExecutor` converts into `TaskState.TASK_STATE_INPUT_REQUIRED`.

### 4. A2A Server Executor (`agents/a2a_app.py`)
- **Choice**: Subclass or wrap `A2aAgentExecutor` in `ADKExecutor(AgentExecutor)` (with `LangGraphExecutor = ADKExecutor` alias if needed):
  - Accepts `build_agent: Callable[[], Awaitable[BaseAgent] | BaseAgent]`.
  - Uses an `asyncio.Lock` in `_ensure()` / `_resolve_runner()` so concurrent cold-start requests invoke `build_agent` at most once and wrap the resulting `BaseAgent` in an `InMemoryRunner` (or `Runner` with `InMemorySessionService`, `InMemoryArtifactService`, `InMemoryMemoryService`, `InMemoryCredentialService`).
  - Wraps execution so any unhandled exception from `build_agent` or runner setup logs the full traceback locally and emits a sanitized `TASK_STATE_FAILED` status (`execution error; see agent logs for task <id>`) rather than leaking internal URLs or stack traces to remote callers.
  - Keeps `build_app(card, executor, *, service=None)` with `TrustedHostMiddleware` so DNS-rebinding protection and custom `AgentCard` definitions remain unchanged.

## Risks / Trade-offs

- **[Risk] `A2aAgentExecutor._resolve_runner()` has no lock by default** → **Mitigation**: Synchronize `_ensure()` with `asyncio.Lock()` in `ADKExecutor` and pass the pre-synchronized async factory (or resolved `Runner`) to `A2aAgentExecutor`.
- **[Risk] Uncaught error in `build_agent()` before `A2aAgentExecutor`'s `try` block or raw `str(e)` in `A2aAgentExecutor`'s failure event leaks internal details** → **Mitigation**: `ADKExecutor.execute()` catches exceptions around `_ensure()` and sanitizes failure status messages.
- **[Risk] `McpToolset` unclosed sessions on teardown** → **Mitigation**: ADK manages MCP sessions per toolset; tests that run offline use stub `BaseLlm` and `FunctionTool` instances without opening network sockets.
