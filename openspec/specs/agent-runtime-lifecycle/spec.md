# agent-runtime-lifecycle Specification

## Purpose
Guarantees concurrency-safe lazy initialization of LangGraph agents, call-time resolution of service endpoint URLs, cross-process locking for shared demo state, and deterministic cleanup of backend client connections.

## Requirements

### Requirement: Single-flight lazy initialization in LangGraphExecutor
The A2A `LangGraphExecutor` SHALL synchronize lazy agent initialization so concurrent cold-start requests build the underlying LangGraph agent at most once.

#### Scenario: Concurrent cold-start requests share one agent build
- **WHEN** multiple coroutines concurrently invoke `_ensure()` on a newly constructed `LangGraphExecutor`
- **THEN** the `build_agent` factory is awaited exactly once and all callers receive the same agent instance

### Requirement: Call-time resolution of MCP and A2A endpoint URLs
The Observer agent, Operator agent, remediation tool, and incident runner script SHALL read `K8S_RO_URL`, `PROMETHEUS_ALERTS_URL`, `ES_LOGS_URL`, `K8S_WRITE_URL`, `SHELL_URL`, `OPERATOR_URL`, and `OBSERVER_URL` from the environment when constructing clients or making calls rather than freezing them at module import time.

#### Scenario: Environment variable overrides after module import take effect
- **WHEN** `K8S_RO_URL`, `K8S_WRITE_URL`, or `OPERATOR_URL` is modified in `os.environ` after the agent modules have been imported
- **THEN** newly constructed MCP clients and remediation calls use the updated environment URLs

### Requirement: Cross-process file locking and atomic temp-file cleanup for demo state
The demo state store SHALL acquire both an in-process thread lock and an advisory cross-process `fcntl.flock` lock before reading or writing the state file, and SHALL remove any temporary file if atomic replacement fails.

#### Scenario: State load, update, and reset serialize across threads and processes
- **WHEN** `load()`, `update()`, or `reset()` accesses the demo state file
- **THEN** it holds both the thread lock and an exclusive `fcntl.flock` on `<state_file>.lock` for the duration of the operation and cleans up temporary files on error

### Requirement: Managed ReAct graph and backend client lifecycles
The runtime SHALL construct ReAct agents directly from `StateGraph(MessagesState)`, `ToolNode`, and `tools_condition` without calling deprecated `langgraph.prebuilt.create_react_agent`, and real Kubernetes and Elasticsearch backends SHALL close their underlying HTTP clients after each operation.

#### Scenario: Agent graph executes tool loop and returns final response
- **WHEN** an Observer or Operator agent is built via `build_react_agent` with a system prompt, tools, and optional checkpointer/interrupt configuration
- **THEN** the compiled graph invokes the model with the system message and routes tool calls through `ToolNode` until completion
