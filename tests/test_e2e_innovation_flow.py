"""End-to-end integration test for the full SRE agent innovation workflow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from agents.observer import remediation_tool
from common.approval import approval_store
from common.circuit_breaker import circuit_breaker
from common.notifications.notifier import IncidentCard
from common.schemas import RemediationProposal, RollbackAction
from mcp_servers import demo_state, k8s_readonly, k8s_write
from services.gateway import create_gateway_app, deduplicator


@pytest.mark.anyio
async def test_end_to_end_alert_to_remediation_flow():
    """Verify alert -> gateway -> structured remediation -> circuit breaker -> notification."""
    demo_state.reset()
    circuit_breaker.reset()
    deduplicator.reset()
    approval_store.reset()

    # 1. Ingest firing Alertmanager alert
    gateway_client = TestClient(create_gateway_app(), base_url="http://localhost")
    alert_payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "service": "payments-api",
                    "namespace": "payments",
                    "severity": "critical",
                },
                "annotations": {"summary": "payments-api pods are crashing with OOMKilled"},
                "fingerprint": "e2e-fp-01",
            }
        ],
    }

    notifications_sent: list[IncidentCard] = []

    async def mock_notify(card: IncidentCard) -> bool:
        notifications_sent.append(card)
        return True

    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock) as mock_dispatch:
        res = gateway_client.post("/api/v1/alerts", json=alert_payload)
        assert res.status_code == 202
        assert mock_dispatch.called

    # 2. Simulate Observer requesting remediation with structured RemediationProposal
    proposal = RemediationProposal(
        action_type="restart",
        target_resource="payments-api",
        target_namespace="payments",
        reason="OOMKilled pods observed",
        rollback_action=RollbackAction(
            action_type="restart",
            target_resource="payments-api",
            target_namespace="payments",
            description="Re-restart to revert",
        ),
    )

    with (
        patch("agents.observer.remediation_tool.call_agent", new_callable=AsyncMock) as mock_call,
        patch("common.notifications.notifier.CompositeNotifier.notify", side_effect=mock_notify),
    ):
        mock_call.return_value = "Restarted deployment payments-api in namespace payments"
        report = await remediation_tool._request_remediation(proposal.model_dump_json())
        assert "Restarted deployment" in report
        assert any(c.phase == "remediation_proposed" for c in notifications_sent)
        assert any(c.phase == "action_executing" for c in notifications_sent)

    # 3. Simulate Operator mutating cluster
    result = k8s_write.restart_deployment("payments-api", "payments")
    assert "recovered" in result["result"]
    assert all(p["ready"] for p in k8s_readonly.list_pods("payments"))

    # 4. Confirm circuit breaker prevents immediate repeated mutation
    with pytest.raises(Exception, match="Circuit breaker active"):
        k8s_write.restart_deployment("payments-api", "payments")
