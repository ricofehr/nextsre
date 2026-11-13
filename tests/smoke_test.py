"""Offline demo check - no LLM, no real cluster, no pytest.

A dependency-free entry point (``python -m tests.smoke_test``) that shows the
incident flow works end to end:

* the MCP tools report the synthetic incident
* a restart flips the shared state from incident -> healthy, and the read side
  confirms it

It deliberately does **not** assert the policy harness. Those assertions live in
``tests/test_harness.py``, where each case is a regression for a bypass the
harness once accepted. A second, thinner copy here would drift out of step with
the real allowlists and print a reassuring tick for a boundary nobody checked.
Run ``pytest`` before trusting the harness.
"""

from __future__ import annotations

import os
import tempfile

# Use an isolated temp state file so the test never touches a real one.
os.environ["DEMO_STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "state.json")
os.environ["DEMO_MODE"] = "1"


def check_read_tools() -> None:
    from mcp_servers import demo_state, k8s_readonly, loki_logs, prometheus_alerts, tempo_traces

    demo_state.reset()
    pods = k8s_readonly.list_pods("payments")
    assert pods and all(p["reason"] == "CrashLoopBackOff" for p in pods), pods
    assert "OOMKilled" in k8s_readonly.get_pod_logs("payments-api-00", "payments")
    assert any(a["severity"] == "critical" for a in prometheus_alerts.active_alerts("warning"))
    docs = loki_logs.search_logs("error", service="payments-api")
    assert any(d["level"] == "ERROR" for d in docs)
    trace_id = docs[0]["trace_id"]
    traces = tempo_traces.search_traces(service="payments-api", status="error")
    assert any(t["trace_id"] == trace_id for t in traces)
    assert tempo_traces.get_trace(trace_id)["status"] == "error"
    print("✓ read tools report the incident")


def check_recovery_loop() -> None:
    from mcp_servers import demo_state, k8s_readonly, k8s_write, tempo_traces

    demo_state.reset()
    assert all(not p["ready"] for p in k8s_readonly.list_pods("payments"))
    result = k8s_write.restart_deployment("payments-api", "payments")
    assert "recovered" in result["result"]
    assert all(p["ready"] for p in k8s_readonly.list_pods("payments"))
    assert all(
        a["state"] == "resolved"
        for a in demo_state.load()["alerts"]
        if a["service"] == "payments-api"
    )
    assert tempo_traces.search_traces(service="payments-api", status="error") == []
    print("✓ restart resolves the incident; read side confirms recovery")


if __name__ == "__main__":
    check_read_tools()
    check_recovery_loop()
    print("\nDEMO FLOW OK - run `pytest` for the policy-harness suite")
