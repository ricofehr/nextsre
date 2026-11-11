# Spec Delta

## MODIFIED Requirements

### Requirement: Call-time resolution of MCP and A2A endpoint URLs
The Observer agent, Operator agent, remediation tool, and incident runner script SHALL read `K8S_RO_URL`, `PROMETHEUS_ALERTS_URL`, `ES_LOGS_URL`, `K8S_WRITE_URL`, `SHELL_URL`, `OPERATOR_URL`, and `OBSERVER_URL` from the environment when constructing MCP toolsets or making calls rather than freezing them at module import time.

#### Scenario: Environment variable overrides after module import take effect
- **WHEN** `K8S_RO_URL`, `K8S_WRITE_URL`, or `OPERATOR_URL` is modified in `os.environ` after the agent modules have been imported
- **THEN** newly constructed MCP toolsets and remediation calls use the updated environment URLs

## ADDED Requirements

### Requirement: Single-flight lazy initialization in ADK A2A executor
The A2A agent executor SHALL synchronize lazy agent and runner initialization so concurrent cold-start requests build the underlying ADK agent and runner at most once, and SHALL sanitize unexpected execution errors before publishing a failed A2A task status.

#### Scenario: Concurrent cold-start requests share one agent build
- **WHEN** multiple coroutines concurrently invoke `_ensure()` (or `_resolve_runner()`) on a newly constructed `ADKExecutor`
- **THEN** the `build_agent` factory is awaited exactly once and all callers receive the same runner instance

### Requirement: Managed ADK agent, tool confirmation gate, and backend client lifecycles
The runtime SHALL construct Observer and Operator agents using ADK `LlmAgent` with `OpenAILlm` configured from `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY`, SHALL enforce human approval on write tools via ADK tool confirmation (`require_confirmation=True`) when `REQUIRE_APPROVAL=1`, and real Kubernetes and Elasticsearch backends SHALL close their underlying HTTP clients after each operation.

#### Scenario: ADK agent executes tool loop and returns final response
- **WHEN** an agent is built via `build_adk_agent` (or `build_react_agent`) with a system prompt and tools
- **THEN** running the agent invokes the model with the system instruction, executes requested tool calls, and yields the final response

#### Scenario: Operator pauses for tool confirmation when REQUIRE_APPROVAL is enabled
- **WHEN** an agent is built with `require_confirmation=True` (or `REQUIRE_APPROVAL=1` on the Operator) and the model attempts to call a gated tool
- **THEN** the tool does not execute immediately and the run requests confirmation (`adk_request_confirmation`)

## REMOVED Requirements

### Requirement: Single-flight lazy initialization in LangGraphExecutor
**Reason**: Replaced by `Single-flight lazy initialization in ADK A2A executor` as LangGraph is removed in favor of Google ADK.
**Migration**: Use `ADKExecutor` in `agents/a2a_app.py`.

### Requirement: Managed ReAct graph and backend client lifecycles
**Reason**: Replaced by `Managed ADK agent, tool confirmation gate, and backend client lifecycles` as LangGraph's `StateGraph` and `ToolNode` are replaced by ADK's `LlmAgent` and `Runner`.
**Migration**: Use `build_adk_agent` (or `build_react_agent` alias) in `common/llm.py`.
