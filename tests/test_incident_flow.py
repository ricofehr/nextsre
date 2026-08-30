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
    assert pods and all(p["phase"] == "CrashLoopBackOff" for p in pods)

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
    finally:
        server.should_exit = True


def _await_card(url: str, attempts: int = 50) -> None:
    for _ in range(attempts):
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    pytest.fail(f"agent card never came up at {url}")
