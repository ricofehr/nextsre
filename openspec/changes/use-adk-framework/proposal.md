# Proposal

## Why

The Observer and Operator agents currently rely on four separate packages (`langgraph`, `langchain-core`, `langchain-openai`, `langchain-mcp-adapters`) and a custom `LangGraphExecutor` bridge to integrate MCP tools, an OpenAI-compatible local LLM, and A2A protocol serving. Replacing the LangChain/LangGraph stack with Google's Agent Development Kit (`google-adk[a2a,mcp,openai]`) consolidates agent construction, streamable-HTTP MCP toolsets, tool confirmation gates, and native A2A execution into a single framework designed around the A2A SDK and MCP.

## What Changes

- **BREAKING**: Remove `langgraph`, `langchain-mcp-adapters`, `langchain-openai`, and `langchain-core` from `pyproject.toml` and replace them with `google-adk[a2a,mcp,openai]`.
- Replace `ChatOpenAI` and the custom `StateGraph` builder in `common/llm.py` with `OpenAILlm` (`google.adk.integrations.openai`) and an ADK `LlmAgent` factory (`build_adk_agent` / `build_react_agent`).
- Replace `MultiServerMCPClient` in `agents/observer/observer_agent.py` and `agents/operator/operator_agent.py` with ADK `McpToolset` instances configured via `StreamableHTTPConnectionParams`.
- Replace LangGraph's `interrupt_before=["tools"]` + `InMemorySaver` checkpointer for `REQUIRE_APPROVAL=1` with ADK's native tool confirmation gate (`require_confirmation=True` on Operator's `McpToolset` instances).
- Replace `@tool` from `langchain_core.tools` on `request_remediation` in `agents/observer/remediation_tool.py` with an ADK `FunctionTool` (or plain async callable wrapped by ADK).
- Replace `LangGraphExecutor` in `agents/a2a_app.py` with an ADK `A2aAgentExecutor`-based executor (`ADKExecutor`, aliased as `LangGraphExecutor` for backward compatibility if needed, or updated across servers and tests) backed by an `InMemoryRunner` with single-flight lazy initialization and sanitized failure messages.
- Update unit tests (`tests/test_incident_flow.py`, `tests/test_privilege_boundary.py`) and project documentation (`README.md`, `CLAUDE.md`) to reflect the ADK runtime.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `agent-runtime-lifecycle`: Replace LangGraph-specific executor (`LangGraphExecutor`) and `StateGraph` / `ToolNode` requirements with ADK `A2aAgentExecutor` / `Runner` single-flight lazy initialization, `LlmAgent` + `McpToolset` construction, and ADK tool-confirmation gating.

## Impact

- **Branch**: `feat/use-adk-framework`
- **Dependencies**: `pyproject.toml` drops `langgraph`, `langchain-mcp-adapters`, `langchain-openai`, `langchain-core`; adds `google-adk[a2a,mcp,openai]==2.14.0`.
- **Code**: `common/llm.py`, `agents/a2a_app.py`, `agents/observer/observer_agent.py`, `agents/observer/remediation_tool.py`, `agents/observer/server.py`, `agents/operator/operator_agent.py`, `agents/operator/server.py`.
- **Tests & Docs**: `tests/test_incident_flow.py`, `tests/test_privilege_boundary.py`, `README.md`, `CLAUDE.md`.
