# Spec Delta

## MODIFIED Requirements

### Requirement: Call-time resolution of MCP and A2A endpoint URLs
The Observer agent, Operator agent, remediation tool, and incident runner script SHALL read `K8S_RO_URL`, `PROMETHEUS_ALERTS_URL`, `LOKI_LOGS_URL`, `TEMPO_TRACES_URL`, `K8S_WRITE_URL`, `SHELL_URL`, `OPERATOR_URL`, and `OBSERVER_URL` from the environment when constructing MCP toolsets or making calls rather than freezing them at module import time.

#### Scenario: Environment variable overrides after module import take effect
- **WHEN** `K8S_RO_URL`, `LOKI_LOGS_URL`, `TEMPO_TRACES_URL`, `K8S_WRITE_URL`, or `OPERATOR_URL` is modified in `os.environ` after the agent modules have been imported
- **THEN** newly constructed MCP toolsets and remediation calls use the updated environment URLs

### Requirement: Managed ADK agent, tool confirmation gate, and backend client lifecycles
The runtime SHALL construct Observer and Operator agents using ADK `LlmAgent` with `OpenAILlm` configured from `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY`, SHALL enforce human approval on write tools via ADK tool confirmation (`require_confirmation=True`) when `REQUIRE_APPROVAL=1`, and real Kubernetes, Loki, and Tempo backends SHALL close their underlying HTTP/API clients after each operation.

#### Scenario: ADK agent executes tool loop and returns final response
- **WHEN** an agent is built via `build_adk_agent` (or `build_react_agent`) with a system prompt and tools
- **THEN** running the agent invokes the model with the system instruction, executes requested tool calls, and yields the final response

#### Scenario: Operator pauses for tool confirmation when REQUIRE_APPROVAL is enabled
- **WHEN** an agent is built with `require_confirmation=True` (or `REQUIRE_APPROVAL=1` on the Operator) and the model attempts to call a gated tool
- **THEN** the tool does not execute immediately and the run requests confirmation (`adk_request_confirmation`)
