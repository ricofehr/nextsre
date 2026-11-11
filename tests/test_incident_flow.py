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
    """Concurrent cold-start requests must not build the agent or runner twice."""
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest

    from agents.a2a_app import ADKExecutor

    class IdleLlm(BaseLlm):
        model: str = "idle"

        async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
            return
            yield

    calls = 0

    async def slow_build():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return LlmAgent(name="test_agent", model=IdleLlm(), instruction="noop")

    executor = ADKExecutor(slow_build)

    async def run_concurrent():
        return await asyncio.gather(*(executor._ensure_runner() for _ in range(5)))

    runners = asyncio.run(run_concurrent())
    assert calls == 1
    assert len({id(r) for r in runners}) == 1


def test_adk_executor_sanitizes_internal_errors():
    """ADKExecutor must not leak raw exception messages or internal URLs on failure."""
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
    from a2a.utils import TransportProtocol
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest

    from agents.a2a_app import ADKExecutor, build_app, call_agent

    class FailingLlm(BaseLlm):
        model: str = "failing"

        async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
            raise RuntimeError("secret internal url http://10.0.0.99:8201/mcp failed")
            yield

    def build_failing():
        return LlmAgent(name="failing_agent", model=FailingLlm(), instruction="fail")

    card = AgentCard(
        name="Failing",
        description="test",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(url="http://127.0.0.1:9397", protocol_binding=TransportProtocol.JSONRPC)
        ],
        skills=[AgentSkill(id="fail", name="Fail", description="fail", tags=["t"])],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(card, ADKExecutor(build_failing)),
            host="127.0.0.1",
            port=9397,
            log_level="warning",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    try:
        _await_card("http://127.0.0.1:9397/.well-known/agent-card.json")
        statuses: list[str] = []
        result = asyncio.run(
            call_agent("http://127.0.0.1:9397", "trigger error", on_status=statuses.append)
        )
        combined = " ".join([*statuses, result])
        assert "10.0.0.99" not in combined
        assert "secret internal url" not in combined
        assert any("execution error; see agent logs for task" in s for s in statuses)
    finally:
        server.should_exit = True


def test_adk_executor_retries_build_after_invalid_agent_and_fails_on_unreachable_toolset():
    """_ensure does not wedge on invalid build return, and unreachable toolsets fail closed."""
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.tools.base_toolset import BaseToolset

    from agents.a2a_app import ADKExecutor

    class IdleLlm(BaseLlm):
        model: str = "idle"

        async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
            return
            yield

    class BrokenToolset(BaseToolset):
        async def get_tools(self, readonly_context=None):
            raise ConnectionError("mcp server unreachable")

    attempts = 0

    def flaky_build():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return object()  # invalid type on first call
        return LlmAgent(
            name="test_agent",
            model=IdleLlm(),
            instruction="noop",
            tools=[BrokenToolset()],
        )

    executor = ADKExecutor(flaky_build)
    with pytest.raises(TypeError, match="Expected BaseAgent or Runner"):
        asyncio.run(executor._ensure())

    # Second attempt succeeds because _ensure did not cache the invalid object.
    agent = asyncio.run(executor._ensure())
    assert agent.name == "test_agent"

    # Unreachable toolset raises in _verify_toolsets instead of silently returning [].
    with pytest.raises(ConnectionError, match="mcp server unreachable"):
        asyncio.run(executor._verify_toolsets())


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
    """build_react_agent runs model -> FunctionTool -> model, passes instruction,
    and supports require_confirmation."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from common.llm import build_react_agent

    def ping(host: str) -> str:
        """Return pong for host."""
        return f"pong:{host}"

    class StubModel(BaseLlm):
        model: str = "stub"
        turns: int = 0
        seen_system: list[str] = []

        async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
            assert llm_request.config and llm_request.config.system_instruction
            self.seen_system.append(str(llm_request.config.system_instruction))
            self.turns += 1
            if self.turns == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_function_call(name="ping", args={"host": "db"})],
                    )
                )
                return
            last_fr = llm_request.contents[-1].parts[0].function_response.response
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=f"done after {last_fr['result']}")],
                )
            )

    async def _run(agent, text: str):
        runner = InMemoryRunner(agent=agent, app_name="test_app")
        session = await runner.session_service.create_session(app_name="test_app", user_id="u1")
        events = []
        async for ev in runner.run_async(
            user_id="u1",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=text)]),
        ):
            events.append(ev)
        return events

    model = StubModel()
    agent = build_react_agent(model, [ping], prompt="system rules")
    events = asyncio.run(_run(agent, "check db"))
    assert events[-1].content.parts[0].text == "done after pong:db"
    assert len(model.seen_system) == 2
    assert all("system rules" in s for s in model.seen_system)

    gated = build_react_agent(
        StubModel(),
        [ping],
        prompt="system rules",
        require_confirmation=True,
    )
    paused_events = asyncio.run(_run(gated, "check db"))
    confirm_calls = [
        part.function_call.name
        for ev in paused_events
        if ev.content and ev.content.parts
        for part in ev.content.parts
        if part.function_call
    ]
    assert "adk_request_confirmation" in confirm_calls
    assert any(ev.long_running_tool_ids for ev in paused_events)


def _await_card(url: str, attempts: int = 50) -> None:
    for _ in range(attempts):
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    pytest.fail(f"agent card never came up at {url}")
