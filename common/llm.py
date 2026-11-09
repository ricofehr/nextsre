"""Shared LLM factory and ReAct StateGraph builder.

Both agents talk to a local, OpenAI-compatible endpoint (Ollama or
llama.cpp's ``llama-server``). Nothing here is Anthropic/OpenAI specific -
``ChatOpenAI`` is just the OpenAI *wire format* client, pointed at localhost.

``build_react_agent`` constructs the two-node ``agent <-> tools`` StateGraph
directly instead of calling ``langgraph.prebuilt.create_react_agent``
(deprecated since LangGraph 1.0 in favour of ``langchain.agents.create_agent``,
which would pull in the full ``langchain`` package).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition


def local_llm(model: str | None = None) -> ChatOpenAI:
    """Return a chat model bound to the local inference server.

    Configured entirely through environment variables so the same image works
    against Ollama, llama-server, vLLM, etc.:

    * ``LLM_BASE_URL``  - OpenAI-compatible base url
                          (Ollama: ``http://host:11434/v1``,
                           llama-server: ``http://host:8080/v1``)
    * ``LLM_MODEL``     - model name *as served* (e.g. ``gemma3:27b``)
    * ``LLM_API_KEY``   - usually ignored by local servers, but the client
                          still requires a non-empty string
    """
    return ChatOpenAI(
        model=model or os.environ.get("LLM_MODEL", "gemma3:27b"),
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("LLM_API_KEY", "local"),
        temperature=0,  # deterministic, boring reasoning for ops work
        timeout=120,
    )


def build_react_agent(
    model: ChatOpenAI,
    tools: Sequence[BaseTool],
    *,
    prompt: str,
    checkpointer: BaseCheckpointSaver | None = None,
    interrupt_before: list[str] | None = None,
):
    """Compile a minimal ReAct StateGraph over ``model`` and ``tools``."""
    bound = model.bind_tools(tools)
    system = SystemMessage(content=prompt)

    async def call_model(state: MessagesState) -> dict:
        response = await bound.ainvoke([system, *state["messages"]])
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)
