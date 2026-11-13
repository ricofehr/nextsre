"""Shared A2A plumbing for both agents (a2a-sdk 1.x, proto types, Google ADK).

Contains:
* ``build_app``        - assemble a Starlette ASGI app from a card + executor
* ``ADKExecutor``      - bridge an ADK agent builder to A2A via ADK's
                         ``A2aAgentExecutor`` and ``InMemoryRunner``, with
                         single-flight lazy initialization and sanitized errors
* ``call_agent``       - act as an A2A client: resolve a remote card, send a
                         message, and collect the streamed result
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable
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
from a2a.types import AgentCard, Part, TaskState, TaskStatusUpdateEvent
from a2a.utils import DEFAULT_RPC_URL
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.agents.base_agent import BaseAgent
from google.adk.runners import InMemoryRunner, Runner
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from common.a2a_helpers import render_event, user_text_request
from common.serving import allowed_hosts
from common.telemetry import extract_trace_context, get_tracer, inject_trace_headers

log = logging.getLogger(__name__)


class _TelemetryMiddleware:
    """Extract incoming W3C traceparent headers and wrap HTTP handling in a server span."""

    def __init__(self, app: Any, *, service: str | None = None) -> None:
        self.app = app
        self._service = service or "a2a"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers") or []
        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in raw_headers
            if isinstance(k, bytes) and isinstance(v, bytes)
        }
        ctx = extract_trace_context(headers)
        attrs = {
            "rpc.system": "a2a",
            "http.request.method": str(scope.get("method", "")),
            "url.path": str(scope.get("path", "")),
        }
        with get_tracer(self._service).start_as_current_span(
            "a2a.request",
            context=ctx,
            kind=SpanKind.SERVER,
            attributes=attrs,
        ):
            await self.app(scope, receive, send)


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
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
        Middleware(_TelemetryMiddleware, service=service or card.name),
    ]
    return Starlette(routes=routes, middleware=middleware)


# --------------------------------------------------------------------------- #
#  Server: ADK -> A2A executor with single-flight lazy init & sanitized errors
# --------------------------------------------------------------------------- #
def _has_adk_error(event: TaskStatusUpdateEvent) -> bool:
    for meta in (
        event.metadata,
        event.status.message.metadata if event.status.message else None,
    ):
        if meta and any("error_code" in k for k in meta):
            return True
    return False


class _SanitizingEventQueue(EventQueue):
    """Wrap an EventQueue to sanitize raw exception messages on error events."""

    def __init__(self, inner: EventQueue, task_id: str | None) -> None:
        self._inner = inner
        self._task_id = task_id

    async def enqueue_event(self, event: Any) -> None:
        if isinstance(event, TaskStatusUpdateEvent) and (
            event.status.state == TaskState.TASK_STATE_FAILED or _has_adk_error(event)
        ):
            if event.status.message and event.status.message.parts:
                tid = event.task_id or self._task_id or "unknown"
                del event.status.message.parts[:]
                event.status.message.parts.append(
                    Part(text=f"execution error; see agent logs for task {tid}")
                )
        await self._inner.enqueue_event(event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ADKExecutor(AgentExecutor):
    """Wrap an async/sync ADK agent builder in ADK's ``A2aAgentExecutor``."""

    def __init__(self, build_agent: Callable[[], Awaitable[Any] | Any]):
        self._build = build_agent
        self._agent: Any = None
        self._runner: Runner | None = None
        self._lock = asyncio.Lock()
        self._delegate = A2aAgentExecutor(runner=self._ensure_runner)

    async def _ensure(self) -> Any:
        if self._runner is None:
            async with self._lock:
                if self._runner is None:
                    built = self._build()
                    agent = await built if inspect.isawaitable(built) else built
                    if isinstance(agent, Runner):
                        runner = agent
                    elif isinstance(agent, BaseAgent):
                        runner = InMemoryRunner(
                            agent=agent,
                            app_name=agent.name or "sre_agent",
                        )
                    else:
                        raise TypeError(f"Expected BaseAgent or Runner, got {type(agent)}")
                    self._agent = agent
                    self._runner = runner
        return self._agent

    async def _ensure_runner(self) -> Runner:
        await self._ensure()
        assert self._runner is not None
        return self._runner

    async def _verify_toolsets(self) -> None:
        from google.adk.tools.base_toolset import BaseToolset

        tools = getattr(self._agent, "tools", None) or []
        for tool in tools:
            if isinstance(tool, BaseToolset):
                await tool.get_tools()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        with get_tracer("a2a.executor").start_as_current_span(
            "a2a.execute_task",
            attributes={"rpc.system": "a2a", "a2a.task_id": context.task_id or "unknown"},
        ):
            try:
                await self._ensure_runner()
                await self._verify_toolsets()
                safe_queue = _SanitizingEventQueue(event_queue, context.task_id)
                await self._delegate.execute(context, safe_queue)
            except Exception:  # surface failures as a failed Task without leaking internals
                task = context.current_task
                if task is None and context.message is not None:
                    task = new_task_from_user_message(context.message)
                    await event_queue.enqueue_event(task)
                task_id = task.id if task is not None else (context.task_id or "unknown")
                context_id = (
                    task.context_id if task is not None else (context.context_id or "unknown")
                )
                log.exception("agent execution failed for task %s", task_id)
                try:
                    updater = TaskUpdater(event_queue, task_id, context_id)
                    await updater.failed(
                        updater.new_agent_message(
                            [Part(text=f"execution error; see agent logs for task {task_id}")]
                        )
                    )
                except Exception:
                    log.exception("could not mark task %s failed", task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await self._delegate.cancel(context, event_queue)


LangGraphExecutor = ADKExecutor


# --------------------------------------------------------------------------- #
#  Client: call another A2A agent and collect the streamed result
# --------------------------------------------------------------------------- #
async def call_agent(base_url: str, text: str, *, on_status=None, timeout: float = 180) -> str:
    """Send ``text`` to the agent at ``base_url`` and return its final report.

    ``on_status`` (optional) is called with each interim status string so the
    caller can surface progress.
    """
    with get_tracer("a2a.client").start_as_current_span(
        "a2a.call_agent",
        kind=SpanKind.CLIENT,
        attributes={"rpc.system": "a2a"},
    ):
        headers = inject_trace_headers()
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as http:
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
