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

from common.circuit_breaker import circuit_breaker
from mcp_servers import (
    backends,
    demo_state,
    k8s_readonly,
    k8s_write,
    loki_logs,
    prometheus_alerts,
    tempo_traces,
)


def test_read_tools_report_the_incident():
    pods = k8s_readonly.list_pods("payments")
    # phase is the K8s pod phase (a crash-looping pod is Running); the
    # CrashLoopBackOff signal is the container waiting reason.
    assert pods and all(p["phase"] == "Running" for p in pods)
    assert all(p["reason"] == "CrashLoopBackOff" for p in pods)
    assert not any(p["ready"] for p in pods)

    pod_logs = k8s_readonly.get_pod_logs("payments-api-00", "payments")
    assert "OOMKilled" in pod_logs
    assert "trace_id=" in pod_logs

    events = k8s_readonly.recent_warning_events("payments")
    assert any(e["reason"] == "BackOff" for e in events)

    alerts = prometheus_alerts.active_alerts("warning")
    assert any(a["severity"] == "critical" for a in alerts)

    docs = loki_logs.search_logs("error", service="payments-api")
    assert any(d["level"] == "ERROR" for d in docs)
    trace_ids = {d["trace_id"] for d in docs if d.get("trace_id")}
    assert trace_ids
    trace_id = next(iter(trace_ids))
    assert len(trace_id) == 32

    traces = tempo_traces.search_traces(service="payments-api", status="error")
    assert any(t["trace_id"] == trace_id and t["status"] == "error" for t in traces)

    detail = tempo_traces.get_trace(trace_id)
    assert detail["trace_id"] == trace_id
    assert detail["status"] == "error"
    assert any(s["status"] == "error" for s in detail["spans"])


def test_severity_floor_filters_alerts():
    assert all(a["severity"] == "critical" for a in prometheus_alerts.active_alerts("critical"))


def test_log_search_filters_by_content():
    """regression: a query of "error" used to return every document, healthy ones included."""
    docs = loki_logs.search_logs("OOMKilled", service="payments-api")
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
    assert tempo_traces.search_traces(service="payments-api", status="error") == []
    ok_traces = tempo_traces.search_traces(service="payments-api", status="ok")
    assert ok_traces and all(t["status"] == "ok" for t in ok_traces)


def test_scale_updates_shared_state():
    k8s_write.restart_deployment("web", "web")
    circuit_breaker.reset()
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
    assert loki_logs.search_logs("error", service="payments-api", size=0) == []
    assert tempo_traces.search_traces(service="payments-api", limit=0) == []
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


def test_telemetry_is_noop_when_unset_and_configures_otlp_when_set(monkeypatch):
    """init_telemetry is a no-op by default and configures OTLP gRPC/HTTP when set."""
    from common import telemetry

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert telemetry.init_telemetry("observer") is False

    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-collector.kube-tracing.svc.cluster.local:4317",
    )
    assert telemetry.init_telemetry("observer") is True
    provider = telemetry.current_tracer_provider()
    assert provider is not None
    assert provider.resource.attributes.get("service.name") == "observer"
    provider.shutdown()

    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-collector.kube-tracing.svc.cluster.local:4318",
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "custom-observer")
    assert telemetry.init_telemetry("observer") is True
    provider_http = telemetry.current_tracer_provider()
    assert provider_http is not None
    assert provider_http.resource.attributes.get("service.name") == "custom-observer"
    provider_http.shutdown()
    telemetry.set_tracer_provider_for_testing(None)


def test_a2a_round_trip_propagates_w3c_traceparent():
    """call_agent injects W3C traceparent and build_app extracts it into the server span."""
    from a2a.helpers import new_task_from_user_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.tasks import TaskUpdater
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part
    from a2a.utils import TransportProtocol
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agents.a2a_app import build_app, call_agent
    from common import telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.set_tracer_provider_for_testing(provider)

    seen_server_trace_ids: list[str] = []

    class TraceEchoExecutor(AgentExecutor):
        async def execute(self, context, event_queue):
            span_ctx = trace.get_current_span().get_span_context()
            seen_server_trace_ids.append(f"{span_ctx.trace_id:032x}")
            task = context.current_task
            if task is None:
                task = new_task_from_user_message(context.message)
                await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.start_work()
            await updater.add_artifact(
                [Part(text=f"traced:{span_ctx.trace_id:032x}")], name="report"
            )
            await updater.complete()

        async def cancel(self, context, event_queue):
            pass

    card = AgentCard(
        name="TraceEcho",
        description="test",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(url="http://127.0.0.1:9396", protocol_binding=TransportProtocol.JSONRPC)
        ],
        skills=[AgentSkill(id="echo", name="Echo", description="echo", tags=["t"])],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(card, TraceEchoExecutor(), service="trace-echo"),
            host="127.0.0.1",
            port=9396,
            log_level="warning",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    try:
        _await_card("http://127.0.0.1:9396/.well-known/agent-card.json")
        with telemetry.get_tracer("test").start_as_current_span("root-incident") as root_span:
            expected_tid = f"{root_span.get_span_context().trace_id:032x}"
            res = asyncio.run(call_agent("http://127.0.0.1:9396", "ping"))
        assert res == f"traced:{expected_tid}"
        assert seen_server_trace_ids == [expected_tid]
        spans = exporter.get_finished_spans()
        span_names = {s.name for s in spans}
        assert {"root-incident", "a2a.call_agent", "a2a.request"} <= span_names
    finally:
        server.should_exit = True
        provider.shutdown()
        telemetry.set_tracer_provider_for_testing(None)


def test_traced_tool_records_tool_name_and_status_without_leaking_arguments():
    """MCP tools emit spans with tool name and status, never raw arguments or secrets."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    from common import telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.set_tracer_provider_for_testing(provider)
    try:
        loki_logs.search_logs("sensitive-prompt-token", service="payments-api")
        with pytest.raises(ValueError, match="hexadecimal"):
            tempo_traces.get_trace("not-hex")

        spans = {s.name: s for s in exporter.get_finished_spans()}
        ok_span = spans["mcp.tool.search_logs"]
        assert ok_span.attributes["mcp.tool.name"] == "search_logs"
        assert ok_span.attributes["mcp.tool.status"] == "ok"
        assert ok_span.status.status_code == StatusCode.OK
        assert "sensitive-prompt-token" not in str(dict(ok_span.attributes))

        err_span = spans["mcp.tool.get_trace"]
        assert err_span.attributes["mcp.tool.name"] == "get_trace"
        assert err_span.attributes["mcp.tool.status"] == "error"
        assert err_span.status.status_code == StatusCode.ERROR
        assert "not-hex" not in str(dict(err_span.attributes))
    finally:
        provider.shutdown()
        telemetry.set_tracer_provider_for_testing(None)


def test_real_loki_logs_queries_gateway_with_basic_auth_and_extracts_trace_id(monkeypatch):
    """RealLogs queries /loki/api/v1/query_range, attaches basic auth, and extracts trace_id."""
    from mcp_servers.backends.real import RealLogs

    monkeypatch.setenv("LOKI_URL", "http://loki-gateway.kube-logcollect.svc.cluster.local")
    monkeypatch.setenv("LOKI_USERNAME", "loki-user")
    monkeypatch.setenv("LOKI_PASSWORD", "loki-pass")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url.copy_with(query=None))
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {
                                "pod_name": "payments-api-00",
                                "namespace_name": "payments",
                                "level": "error",
                            },
                            "values": [
                                [
                                    "1700000000000000000",
                                    '{"message":"connection pool exhausted","level":"ERROR",'
                                    '"trace_id":"0123456789abcdef0123456789abcdef"}',
                                ]
                            ],
                        }
                    ],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    docs = RealLogs().search("pool exhausted", service="payments-api", size=10)
    assert seen["url"] == (
        "http://loki-gateway.kube-logcollect.svc.cluster.local/loki/api/v1/query_range"
    )
    assert seen["auth"] and seen["auth"].startswith("Basic ")
    assert "payments-api" in seen["params"]["query"]
    assert "pool" in seen["params"]["query"] and "exhausted" in seen["params"]["query"]
    assert len(docs) == 1
    assert docs[0]["service"] == "payments-api-00"
    assert docs[0]["level"] == "ERROR"
    assert docs[0]["message"] == "connection pool exhausted"
    assert docs[0]["trace_id"] == "0123456789abcdef0123456789abcdef"


def test_real_tempo_traces_search_and_get_trace(monkeypatch):
    """RealTraces queries Tempo /api/search and /api/traces/<trace_id>."""
    from mcp_servers.backends.real import RealTraces

    monkeypatch.setenv("TEMPO_URL", "http://tempo.kube-tracing.svc.cluster.local:3200")
    trace_id = "0123456789abcdef0123456789abcdef"
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.url.path)
        if request.url.path == "/api/search":
            assert 'resource.service.name = "payments-api"' in request.url.params["q"]
            assert "status = error" in request.url.params["q"]
            return httpx.Response(
                200,
                json={
                    "traces": [
                        {
                            "traceID": trace_id,
                            "rootServiceName": "payments-api",
                            "rootTraceName": "POST /charge",
                            "startTimeUnixNano": "1700000000000000000",
                            "durationMs": 812,
                            "spanSets": [
                                {"spans": [{"spanID": "a" * 16, "status": {"code": "error"}}]}
                            ],
                        }
                    ]
                },
            )
        if request.url.path == f"/api/traces/{trace_id}":
            return httpx.Response(
                200,
                json={
                    "batches": [
                        {
                            "resource": {
                                "attributes": [
                                    {
                                        "key": "service.name",
                                        "value": {"stringValue": "payments-api"},
                                    }
                                ]
                            },
                            "scopeSpans": [
                                {
                                    "spans": [
                                        {
                                            "spanId": "1111111111111111",
                                            "name": "POST /charge",
                                            "startTimeUnixNano": "1700000000000000000",
                                            "endTimeUnixNano": "1700000000812000000",
                                            "status": {
                                                "code": "STATUS_CODE_ERROR",
                                                "message": "OOMKilled",
                                            },
                                            "attributes": [
                                                {
                                                    "key": "k8s.pod.name",
                                                    "value": {"stringValue": "payments-api-00"},
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    backend = RealTraces()
    summaries = backend.search_traces(service="payments-api", status="error", limit=5)
    assert len(summaries) == 1
    assert summaries[0]["trace_id"] == trace_id
    assert summaries[0]["status"] == "error"

    detail = backend.get_trace(trace_id)
    assert detail["trace_id"] == trace_id
    assert detail["status"] == "error"
    assert detail["spans"][0]["attributes"]["k8s.pod.name"] == "payments-api-00"
    assert requests_seen == ["/api/search", f"/api/traces/{trace_id}"]


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_observe_only_observer_completes_without_an_operator(monkeypatch):
    """An observe-only Observer serves a task end to end and never dials the Operator."""
    import socket

    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    from agents.a2a_app import ADKExecutor, build_app, call_agent
    from agents.observer import observer_agent
    from agents.observer.card import observer_card

    class ReportLlm(BaseLlm):
        model: str = "report"

        async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text="report: recommend a rolling restart")]
                )
            )

    operator_dials: list[object] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)

    def count_dials():
        while True:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            operator_dials.append(conn)
            conn.close()

    threading.Thread(target=count_dials, daemon=True).start()

    port = _free_port()
    monkeypatch.setenv("OBSERVE_ONLY", "1")
    monkeypatch.setenv("OPERATOR_URL", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("OBSERVER_PUBLIC_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(observer_agent, "_mcp_toolsets", lambda: {})
    monkeypatch.setattr(observer_agent, "local_llm", lambda: ReportLlm())

    server = uvicorn.Server(
        uvicorn.Config(
            build_app(
                observer_card(), ADKExecutor(observer_agent.build_observer), service="observer"
            ),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    try:
        _await_card(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
        report = asyncio.run(call_agent(f"http://127.0.0.1:{port}", "pods are crash looping"))
        assert report == "report: recommend a rolling restart"
        assert operator_dials == []
    finally:
        server.should_exit = True
        listener.close()
