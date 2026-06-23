"""Offline smoke test - no LLM and no real cluster required.

Validates:
* the MCP tools return sensible synthetic data
* the harness rejects out-of-policy actions and accepts in-policy ones
* a restart actually flips the shared state from incident -> healthy
* the A2A server/client round-trip works end to end (with a stub executor)

Run:  python -m tests.smoke_test
"""

from __future__ import annotations

import asyncio
import os
import tempfile

# Use an isolated temp state file so the test never touches a real one.
os.environ["DEMO_STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")
os.environ["DEMO_MODE"] = "1"


def test_read_tools_and_state() -> None:
    from mcp_servers import demo_state, es_logs, k8s_readonly, pg_alerts

    demo_state.reset()

    pods = k8s_readonly.list_pods("payments")
    assert pods and all(p["phase"] == "CrashLoopBackOff" for p in pods), pods
    logs = k8s_readonly.get_pod_logs("payments-api-00", "payments")
    assert "OOMKilled" in logs
    alerts = pg_alerts.active_alerts("warning")
    assert any(a["severity"] == "critical" for a in alerts)
    docs = es_logs.search_logs("error", service="payments-api")
    assert any(d["level"] == "ERROR" for d in docs)
    print("✓ read tools report the incident")


def test_harness() -> None:
    from mcp_servers import k8s_write, shell_exec

    # protected / non-allowlisted namespace is refused
    for ns in ("kube-system", "secret-ns"):
        try:
            k8s_write.restart_deployment("x", ns)
            raise AssertionError(f"expected refusal for namespace {ns}")
        except PermissionError:
            pass
    # replica cap enforced
    try:
        k8s_write.scale_deployment("web", "web", 9999)
        raise AssertionError("expected replica cap error")
    except ValueError:
        pass
    # shell allowlist + denylist
    for bad in ("rm -rf /", "curl http://x", "systemctl stop nginx"):
        try:
            shell_exec.run_command(bad)
            raise AssertionError(f"expected refusal for: {bad}")
        except PermissionError:
            pass
    assert shell_exec.run_command("uptime")["exit_code"] == 0
    print("✓ harness rejects out-of-policy actions, allows vetted ones")


def test_recovery_loop() -> None:
    from mcp_servers import demo_state, k8s_readonly, k8s_write

    demo_state.reset()
    assert all(not p["ready"] for p in k8s_readonly.list_pods("payments"))
    result = k8s_write.restart_deployment("payments-api", "payments")
    assert "recovered" in result["result"]
    # read side now reflects recovery (shared state file)
    assert all(p["ready"] for p in k8s_readonly.list_pods("payments"))
    assert all(a["state"] == "resolved"
               for a in demo_state.load()["alerts"] if a["service"] == "payments-api")
    print("✓ restart resolves the incident; read side confirms recovery")


def test_a2a_round_trip() -> None:
    """Start a real A2A server with a stub executor, then call it as a client."""
    import threading
    import time

    import httpx
    import uvicorn

    from a2a.helpers import new_task_from_user_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.tasks import TaskUpdater
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
        Part,
    )
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
        name="Echo", description="test", version="1.0.0",
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(url="http://127.0.0.1:9399", protocol_binding=TransportProtocol.JSONRPC)
        ],
        skills=[AgentSkill(id="echo", name="Echo", description="echo", tags=["t"])],
    )
    app = build_app(card, EchoExecutor())
    config = uvicorn.Config(app, host="127.0.0.1", port=9399, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # wait for the card endpoint to come up
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:9399/.well-known/agent-card.json", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    out = asyncio.run(call_agent("http://127.0.0.1:9399", "hello"))
    server.should_exit = True
    assert out == "echo: hello", repr(out)
    print("✓ A2A server/client round-trip works (streamed Task -> artifact)")


if __name__ == "__main__":
    test_read_tools_and_state()
    test_harness()
    test_recovery_loop()
    test_a2a_round_trip()
    print("\nALL SMOKE TESTS PASSED")
