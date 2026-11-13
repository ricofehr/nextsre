"""The invariant the whole system exists to enforce.

The Observer must never hold a tool that mutates infrastructure: its only route
to change is ``request_remediation``, which crosses a process boundary into an
agent with its own harness. Nothing else in the suite would notice if someone
wired a write server into the Observer while adding a feature - every other test
would still pass, and the privilege split would be gone with no signal.
"""

from __future__ import annotations

import mcp_servers.k8s_write as k8s_write
import mcp_servers.shell_exec as shell_exec
from agents.observer import observer_agent
from agents.operator import operator_agent

WRITE_TOOL_NAMES = {"restart_deployment", "scale_deployment", "run_command"}


def _toolset_urls(toolsets: dict) -> str:
    return " ".join(str(ts.connection_params) for ts in toolsets.values())


def test_observer_is_not_wired_to_any_write_server():
    toolsets = observer_agent._mcp_toolsets()
    urls = _toolset_urls(toolsets)
    assert "8201" not in urls and "8202" not in urls, urls
    assert "k8s_write" not in urls and "shell" not in toolsets


def test_observer_reaches_all_read_servers():
    assert set(observer_agent._mcp_toolsets()) == {
        "k8s_ro",
        "prometheus_alerts",
        "loki_logs",
        "tempo_traces",
    }


def test_operator_holds_the_write_servers():
    assert set(operator_agent._mcp_toolsets()) == {"k8s_write", "shell"}


def test_remediation_tool_is_the_observers_only_route_to_change():
    from agents.observer.remediation_tool import request_remediation

    assert request_remediation.name == "request_remediation"
    # It delegates over A2A rather than mutating anything in this process.
    assert not WRITE_TOOL_NAMES & set(dir(observer_agent))


def test_write_tools_live_only_behind_the_operators_servers():
    """Sanity-check the names above still refer to real tools, so this file
    cannot quietly pass because a rename made the assertions vacuous."""
    assert {"restart_deployment", "scale_deployment"} <= set(dir(k8s_write))
    assert "run_command" in dir(shell_exec)


def test_agent_urls_are_resolved_at_call_time(monkeypatch):
    """regression: MCP and Operator URLs were frozen at module import time."""
    from agents.observer import remediation_tool

    monkeypatch.setenv("K8S_RO_URL", "http://custom-k8s-ro:8101/mcp")
    monkeypatch.setenv("LOKI_LOGS_URL", "http://custom-loki-logs:8103/mcp")
    monkeypatch.setenv("TEMPO_TRACES_URL", "http://custom-tempo-traces:8104/mcp")
    monkeypatch.setenv("K8S_WRITE_URL", "http://custom-k8s-write:8201/mcp")
    monkeypatch.setenv("OPERATOR_URL", "http://custom-operator:9301")

    observer_urls = _toolset_urls(observer_agent._mcp_toolsets())
    assert "custom-k8s-ro" in observer_urls
    assert "custom-loki-logs" in observer_urls
    assert "custom-tempo-traces" in observer_urls
    assert "custom-k8s-write" in _toolset_urls(operator_agent._mcp_toolsets())
    assert remediation_tool._operator_url() == "http://custom-operator:9301"


def test_operator_require_approval_enables_toolset_confirmation(monkeypatch):
    """When REQUIRE_APPROVAL=1, build_operator configures require_confirmation on MCP toolsets."""
    import asyncio

    monkeypatch.setenv("REQUIRE_APPROVAL", "1")
    op = asyncio.run(operator_agent.build_operator())
    assert op.tools and all(getattr(t, "require_confirmation", False) is True for t in op.tools)

    monkeypatch.setenv("REQUIRE_APPROVAL", "0")
    op_default = asyncio.run(operator_agent.build_operator())
    assert op_default.tools and all(
        getattr(t, "require_confirmation", False) is False for t in op_default.tools
    )
