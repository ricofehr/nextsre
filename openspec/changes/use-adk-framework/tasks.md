# Tasks

## 1. Dependencies & ADK Runtime Setup

- [x] 1.1 Replace `langgraph`, `langchain-mcp-adapters`, `langchain-openai`, and `langchain-core` with `google-adk[a2a,mcp,openai]==2.14.0` in `pyproject.toml`, install into `.venv`, and verify `import google.adk` succeeds with zero `langchain`/`langgraph` packages imported
- [x] 1.2 Rewrite `common/llm.py` to provide `local_llm()` returning `OpenAILlm` (`google.adk.integrations.openai`) configured from `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY`, and `build_adk_agent` (with `build_react_agent` alias) returning an ADK `LlmAgent` with optional `require_confirmation` tool gating, and verify with unit tests in `tests/test_incident_flow.py`

## 2. A2A Executor & Agent Implementations

- [x] 2.1 Replace `LangGraphExecutor` in `agents/a2a_app.py` with `ADKExecutor` (retaining `LangGraphExecutor = ADKExecutor` alias) backed by ADK's `A2aAgentExecutor` and `InMemoryRunner`, enforcing single-flight `asyncio.Lock` initialization in `_ensure()` and sanitized error reporting, and verify `test_executor_ensure_builds_agent_once_under_concurrency` and `test_a2a_round_trip` pass
- [x] 2.2 Convert `agents/observer/remediation_tool.py`, `agents/observer/observer_agent.py`, `agents/observer/server.py`, `agents/operator/operator_agent.py`, and `agents/operator/server.py` to use ADK `FunctionTool`, `McpToolset` with `StreamableHTTPConnectionParams`, and `require_confirmation=True` when `REQUIRE_APPROVAL=1`, and verify `tests/test_privilege_boundary.py` and `tests/test_incident_flow.py` pass

## 3. Documentation & Verification

- [x] 3.1 Update `README.md`, `CLAUDE.md`, and `DEEP.md` to reflect the Google ADK runtime (`LlmAgent`, `OpenAILlm`, `McpToolset`, `A2aAgentExecutor`, `require_confirmation`) instead of LangChain/LangGraph, and verify `pytest`, `python -m tests.smoke_test`, `black --check .`, `isort --check-only .`, and `flake8` all pass

## Workflow follow-up

- Archive the change after the PR merges (`openspec-archive.yml` or manual archive MR).
