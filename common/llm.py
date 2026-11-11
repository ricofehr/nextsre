"""Shared LLM factory and ADK LlmAgent builder.

Both agents talk to a local, OpenAI-compatible endpoint (Ollama or
llama.cpp's ``llama-server``). Nothing here is Anthropic/OpenAI specific -
``OpenAILlm`` is just the OpenAI *wire format* client, pointed at localhost.

``build_adk_agent`` (aliased as ``build_react_agent``) constructs a Google ADK
``LlmAgent`` over ``model`` and ``tools``, with deterministic ``temperature=0``
and optional ``require_confirmation`` tool gating.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.integrations.openai import OpenAILlm
from google.adk.models.base_llm import BaseLlm
from google.adk.tools import BaseTool, FunctionTool
from google.adk.tools.base_toolset import BaseToolset
from google.genai import types

ToolInput = BaseTool | BaseToolset | Callable[..., Any]


def local_llm(model: str | None = None) -> OpenAILlm:
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
    return OpenAILlm(
        model=model or os.environ.get("LLM_MODEL", "gemma3:27b"),
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("LLM_API_KEY", "local"),
    )


def _normalize_tool(tool: ToolInput, *, require_confirmation: bool) -> BaseTool | BaseToolset:
    if isinstance(tool, (BaseTool, BaseToolset)):
        if require_confirmation and hasattr(tool, "_require_confirmation"):
            tool._require_confirmation = True
        return tool
    return FunctionTool(tool, require_confirmation=require_confirmation)


def build_adk_agent(
    model: BaseLlm | str,
    tools: Sequence[ToolInput],
    *,
    prompt: str,
    name: str = "sre_agent",
    require_confirmation: bool = False,
) -> LlmAgent:
    """Construct an ADK ``LlmAgent`` over ``model`` and ``tools``."""
    normalized_tools = [
        _normalize_tool(t, require_confirmation=require_confirmation) for t in tools
    ]
    return LlmAgent(
        name=name,
        model=model,
        instruction=prompt,
        tools=normalized_tools,
        generate_content_config=types.GenerateContentConfig(temperature=0),
    )


build_react_agent = build_adk_agent
