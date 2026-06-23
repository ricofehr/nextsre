"""The Observer's single bridge to action.

`request_remediation` is an ordinary LangChain tool from the LLM's point of
view, but under the hood it is an A2A client call to the Operator agent. It is
the ONLY tool the Observer has that can change anything - and it crosses a
process/network boundary into an agent with its own, independent harness.
"""

from __future__ import annotations

import os

from langchain_core.tools import tool

from agents.a2a_app import call_agent

OPERATOR_URL = os.environ.get("OPERATOR_URL", "http://localhost:9301")


@tool
async def request_remediation(action: str) -> str:
    """Delegate an infrastructure CHANGE to the operator agent.

    `action` must be ONE precise, self-contained instruction, e.g.
    'rolling-restart deployment payments-api in namespace payments'.
    Returns the operator's report. This is the only way to change anything;
    you cannot mutate the cluster yourself.
    """
    return await call_agent(OPERATOR_URL, action)
