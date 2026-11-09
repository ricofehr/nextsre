"""The four original smoke scenarios: read tools, recovery loop, A2A round-trip.

Offline - no LLM and no real cluster required.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
import uvicorn

from mcp_servers import backends, demo_state, es_logs, k8s_readonly, k8s_write, prometheus_alerts


def test_read_tools_report_the_incident():
    pods = k8s_readonly.list_pods("payments")
    # phase is the K8s pod phase (a crash-looping pod is Running); the
    # CrashLoopBackOff signal is the container waiting reason.
    assert pods and all(p["phase"] == "Running" for p in pods)
    assert all(p["reason"] == "CrashLoopBackOff" for p in pods)
    assert not any(p["ready"] for p in pods)

    assert "OOMKilled" in k8s_readonly.get_pod_logs("payments-api-00", "payments")

    events = k8s_readonly.recent_warning_events("payments")
    assert any(e["reason"] == "BackOff" for e in events)

    alerts = prometheus_alerts.active_alerts("warning")
    assert any(a["severity"] == "critical" for a in alerts)

    docs = es_logs.search_logs("error", service="payments-api")
    assert any(d["level"] == "ERROR" for d in docs)


def test_severity_floor_filters_alerts():
    assert all(a["severity"] == "critical" for a in prometheus_alerts.active_alerts("critical"))


def test_log_search_filters_by_content():
    """regression: a query of "error" used to return every document, healthy ones included."""
    docs = es_logs.search_logs("OOMKilled", service="payments-api")
    assert docs and all("oomkilled" in d["message"].lower() for d in docs)


def test_restart_resolves_the_incident():
    assert all(not p["ready"] for p in k8s_readonly.list_pods("payments"))

    result = k8s_write.restart_deployment("payments-api", "payments")
    assert "recovered" in result["result"]

    # The read side sees it, because both sides share the state file.
    assert all(p["ready"] for p in k8s_readonly.list_pods("payments"))
    assert all(
        a["state"] == "resolved"
        for a in demo_state.load()["alerts"]
        if a["service"] == "payments-api"
    )


def test_scale_updates_shared_state():
    k8s_write.restart_deployment("web", "web")
    k8s_write.scale_deployment("web", "web", 6)
    assert len(k8s_readonly.list_pods("web")) == 6


def test_backend_selection_is_overridable(monkeypatch):
    """The tools resolve their backend per call, so a test can substitute one."""

    class StubK8sRead:
        def list_pods(self, namespace):
            return [{"name": "stub", "namespace": namespace}]

        def pod_logs(self, name, namespace, tail_lines):
            return "stub"

        def warning_events(self, namespace, limit):
            return []

    monkeypatch.setattr(backends, "k8s_read", StubK8sRead)
    assert k8s_readonly.list_pods("payments") == [{"name": "stub", "namespace": "payments"}]


def test_pod_logs_respects_tail_lines():
    assert (
        len(k8s_readonly.get_pod_logs("payments-api-00", "payments", tail_lines=2).split("\n")) == 2
    )


def test_zero_size_returns_nothing():
    """An explicit 0 means "none" - not "one", and not "all"."""
    assert es_logs.search_logs("error", service="payments-api", size=0) == []
    assert k8s_readonly.recent_warning_events("payments", limit=0) == []
    # regression: lines[-0:] is the whole list, so 0 returned every log line.
    assert k8s_readonly.get_pod_logs("payments-api-00", "payments", tail_lines=0) == ""


def test_a2a_round_trip():
    """Start a real A2A server with a stub executor, then call it as a client."""
    from a2a.helpers import new_task_from_user_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.tasks import TaskUpdater
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part
    from a2a.utils import TransportProtocol

    from agents.a2a_app import build_app, call_agent

    class EchoExecutor(AgentExecutor):
        async def execute(self, context, event_queue):
            task = context.current_task
            if task is None:
                task = new_task_from_user_message(context.message)
                await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.start_work()
            await updater.add_artifact(
                [Part(text=f"echo: {context.get_user_input()}")], name="report"
            )
            await updater.complete()

        async def cancel(self, context, event_queue):
            pass

    card = AgentCard(
        name="Echo",
        description="test",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(url="http://127.0.0.1:9399", protocol_binding=TransportProtocol.JSONRPC)
        ],
        skills=[AgentSkill(id="echo", name="Echo", description="echo", tags=["t"])],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(card, EchoExecutor()), host="127.0.0.1", port=9399, log_level="warning"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    try:
        _await_card("http://127.0.0.1:9399/.well-known/agent-card.json")
        assert asyncio.run(call_agent("http://127.0.0.1:9399", "hello")) == "echo: hello"
        # DNS-rebinding Host header must be rejected by TrustedHostMiddleware.
        rebinding = httpx.get(
            "http://127.0.0.1:9399/.well-known/agent-card.json",
            headers={"Host": "evil.attacker.example:9399"},
            timeout=2,
        )
        assert rebinding.status_code == 400
    finally:
        server.should_exit = True


def test_executor_ensure_builds_agent_once_under_concurrency():
    """Concurrent cold-start requests must not build the agent twice."""
    from agents.a2a_app import LangGraphExecutor

    calls = 0

    async def slow_build():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return object()

    executor = LangGraphExecutor(slow_build)

    async def run_concurrent():
        return await asyncio.gather(*(executor._ensure() for _ in range(5)))

    agents = asyncio.run(run_concurrent())
    assert calls == 1
    assert len({id(a) for a in agents}) == 1


def test_demo_state_creates_lock_and_cleans_up_temp_file_on_error(tmp_path, monkeypatch):
    """State access creates the companion .lock file and unlinks temp files on replace failure."""
    import os

    state_path = demo_state.state_file()
    assert state_path.with_suffix(".json.lock").exists()

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        demo_state.update(lambda s: None)

    leftover_tmps = [
        p for p in tmp_path.iterdir() if p.name not in {"state.json", "state.json.lock"}
    ]
    assert leftover_tmps == []


def test_build_react_agent_executes_tool_loop():
    """build_react_agent runs model -> ToolNode -> model, prepends SystemMessage,
    and supports interrupt_before."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from common.llm import build_react_agent

    @tool
    def ping(host: str) -> str:
        """Return pong for host."""
        return f"pong:{host}"

    class StubModel:
        def __init__(self):
            self.turns = 0
            self.seen_system: list[str] = []

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            assert isinstance(messages[0], SystemMessage)
            self.seen_system.append(messages[0].content)
            self.turns += 1
            if self.turns == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"id": "call_1", "name": "ping", "args": {"host": "db"}}],
                )
            return AIMessage(content=f"done after {messages[-1].content}")

    model = StubModel()
    graph = build_react_agent(model, [ping], prompt="system rules")
    out = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="check db")]}))
    assert out["messages"][-1].content == "done after pong:db"
    assert model.seen_system == ["system rules", "system rules"]

    gated = build_react_agent(
        StubModel(),
        [ping],
        prompt="system rules",
        checkpointer=InMemorySaver(),
        interrupt_before=["tools"],
    )
    cfg = {"configurable": {"thread_id": "t1"}}
    paused = asyncio.run(gated.ainvoke({"messages": [HumanMessage(content="check db")]}, cfg))
    assert len(paused["messages"]) == 2
    assert gated.get_state(cfg).next == ("tools",)


def _await_card(url: str, attempts: int = 50) -> None:
    for _ in range(attempts):
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    pytest.fail(f"agent card never came up at {url}")
