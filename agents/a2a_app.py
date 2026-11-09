"""Shared A2A plumbing for both agents (a2a-sdk 1.x, proto types).

Contains:
* ``build_app``        - assemble a Starlette ASGI app from a card + executor
* ``LangGraphExecutor`` - bridge a LangGraph agent to A2A, streaming each graph
                          step as a Task status update and the final answer as
                          an artifact
* ``call_agent``       - act as an A2A client: resolve a remote card, send a
                          message, and collect the streamed result
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCard, Part, TaskState
from a2a.utils import DEFAULT_RPC_URL
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from common.a2a_helpers import render_event, user_text_request
from common.serving import allowed_hosts

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Server: turn a (card, executor) pair into a runnable ASGI app
# --------------------------------------------------------------------------- #
def build_app(
    card: AgentCard,
    executor: AgentExecutor,
    *,
    service: str | None = None,
) -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),  # /.well-known/agent-card.json
        *create_jsonrpc_routes(handler, DEFAULT_RPC_URL),  # JSON-RPC at "/"
    ]
    card_hosts = {
        urlparse(iface.url).hostname
        for iface in card.supported_interfaces
        if iface.url and urlparse(iface.url).hostname
    }
    hosts = sorted(set(allowed_hosts(service)) | card_hosts)
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=hosts)]
    return Starlette(routes=routes, middleware=middleware)


# --------------------------------------------------------------------------- #
#  Server: LangGraph -> A2A executor with streaming Task lifecycle
# --------------------------------------------------------------------------- #
def _summarize_step(payload: dict) -> str | None:
    """Map one LangGraph 'updates' chunk to a short human-readable note."""
    msgs = payload.get("messages") if isinstance(payload, dict) else None
    if not msgs:
        return None
    last = msgs[-1]
    tool_calls = getattr(last, "tool_calls", None)
    if tool_calls:
        names = ", ".join(tc["name"] for tc in tool_calls)
        return f"calling tools: {names}"
    if last.__class__.__name__ == "ToolMessage":
        name = getattr(last, "name", "tool")
        return f"tool '{name}' returned"
    return None


def _is_interrupted(agent, config: dict) -> bool:
    """True when the graph stopped at an interrupt instead of finishing.

    Only graphs built with a checkpointer (REQUIRE_APPROVAL=1) can interrupt, so
    that is the condition tested. Deliberately no ``except`` around
    ``get_state``: swallowing an error here would report "task complete" for a
    remediation that was interrupted and never ran, which is the exact failure
    this check exists to prevent. Let it raise and fail the Task instead.
    """
    if getattr(agent, "checkpointer", None) is None:
        return False
    return bool(agent.get_state(config).next)


class LangGraphExecutor(AgentExecutor):
    """Wrap an async LangGraph builder as a streaming A2A AgentExecutor."""

    def __init__(self, build_agent: Callable[[], Awaitable]):
        self._build = build_agent
        self._agent = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._agent is None:
            async with self._lock:
                if self._agent is None:
                    self._agent = await self._build()
        return self._agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The protocol requires the Task to be enqueued before any status event.
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # Everything below runs inside the try: building the agent connects to the
        # MCP servers and the LLM, and an exception escaping execute() is
        # serialized into the JSON-RPC error verbatim for the remote caller.
        try:
            agent = await self._ensure()
            await updater.start_work(
                updater.new_agent_message([Part(text="received request; investigating")])
            )

            config = {"configurable": {"thread_id": context.context_id}}
            final_text = ""
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": context.get_user_input()}]},
                config=config,
                stream_mode="updates",
            ):
                for payload in chunk.values():
                    note = _summarize_step(payload)
                    if note:
                        await updater.update_status(
                            TaskState.TASK_STATE_WORKING,
                            updater.new_agent_message([Part(text=note)]),
                        )
                    msgs = payload.get("messages") if isinstance(payload, dict) else None
                    if msgs and getattr(msgs[-1], "content", None):
                        final_text = msgs[-1].content

            if _is_interrupted(agent, config):
                # REQUIRE_APPROVAL=1 stops the graph before the tools node. Saying
                # "complete" here would report a remediation that never ran.
                await updater.requires_input(
                    updater.new_agent_message(
                        [Part(text="paused for human approval before running any tool")]
                    )
                )
                return

            await updater.add_artifact([Part(text=final_text)], name="report")
            await updater.complete(updater.new_agent_message([Part(text="task complete")]))
        except Exception:  # surface failures as a failed Task
            # The detail stays in this process' logs: the caller is a remote agent,
            # and raw exception text leaks internal URLs, paths and tool arguments.
            log.exception("agent execution failed for task %s", task.id)
            try:
                await updater.failed(
                    updater.new_agent_message(
                        [Part(text=f"execution error; see agent logs for task {task.id}")]
                    )
                )
            except Exception:
                # The task already reached a terminal state; nothing left to report.
                log.exception("could not mark task %s failed", task.id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


# --------------------------------------------------------------------------- #
#  Client: call another A2A agent and collect the streamed result
# --------------------------------------------------------------------------- #
async def call_agent(base_url: str, text: str, *, on_status=None, timeout: float = 180) -> str:
    """Send ``text`` to the agent at ``base_url`` and return its final report.

    ``on_status`` (optional) is called with each interim status string so the
    caller can surface progress.
    """
    async with httpx.AsyncClient(timeout=timeout) as http:
        card = await A2ACardResolver(http, base_url).get_agent_card()
        client = ClientFactory(ClientConfig(httpx_client=http, streaming=True)).create(card)
        artifact_text = ""
        final_message = ""
        async for event in client.send_message(user_text_request(text)):
            kind, value = render_event(event)
            if kind == "status" and on_status:
                on_status(value)
            elif kind == "artifact":
                artifact_text = value
            elif kind == "message":
                final_message = value
        await client.close()
        return artifact_text or final_message
