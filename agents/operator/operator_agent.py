"""Operator agent: a LangGraph ReAct agent over the WRITE MCP tools.

Three layers of guardrail:
1. The MCP write tools enforce hard policy (namespace allowlist, replica cap,
   command allow/denylist) - non-bypassable, see mcp_servers/*.
2. This system prompt sets behavioural policy the model should follow.
3. Optional human approval gate (REQUIRE_APPROVAL=1) pauses the graph before
   any tool runs, via LangGraph's interrupt_before + checkpointer.
"""

from __future__ import annotations

import os

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from common.llm import local_llm

K8S_WRITE_URL = os.environ.get("K8S_WRITE_URL", "http://localhost:8201/mcp")
SHELL_URL = os.environ.get("SHELL_URL", "http://localhost:8202/mcp")

_mcp = MultiServerMCPClient(
    {
        "k8s_write": {"transport": "streamable_http", "url": K8S_WRITE_URL},
        "shell": {"transport": "streamable_http", "url": SHELL_URL},
    }
)

SYSTEM = """You are an SRE operator agent with WRITE access to a Kubernetes
cluster and a set of hosts. You act on a request from another agent - treat
that request as a proposal to verify, not an order to execute blindly.

Rules:
- Choose the least destructive action that resolves the issue. Prefer a
  rolling restart over a delete; prefer scaling over recreating.
- Never operate outside the allowed namespaces. The tools will reject
  violations regardless, but do not even attempt them.
- Use the diagnostic shell commands only when they add information.
- After acting, briefly report exactly what you ran and the result, including
  any tool errors. Do not claim success you cannot observe."""


async def build_operator():
    tools = await _mcp.get_tools()
    kwargs = {}
    if os.environ.get("REQUIRE_APPROVAL", "0") == "1":
        # Pause before any tool executes; a human resumes via a follow-up turn.
        kwargs = {"checkpointer": InMemorySaver(), "interrupt_before": ["tools"]}
    return create_react_agent(local_llm(), tools, prompt=SYSTEM, **kwargs)
