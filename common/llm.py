"""Shared LLM factory.

Both agents talk to a local, OpenAI-compatible endpoint (Ollama or
llama.cpp's ``llama-server``). Nothing here is Anthropic/OpenAI specific -
``ChatOpenAI`` is just the OpenAI *wire format* client, pointed at localhost.
"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI


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
