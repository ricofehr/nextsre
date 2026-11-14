"""Unit tests for circuit breaker and structured remediation validation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.observer.remediation_tool import validate_remediation_payload
from common.circuit_breaker import CircuitBreakerError, MutationCircuitBreaker
from mcp_servers import k8s_write


def test_validate_remediation_payload_success():
    payload = {
        "action_type": "restart",
        "target_resource": "payments-api",
        "target_namespace": "payments",
        "reason": "CrashLoopBackOff detected",
        "rollback_action": {
            "action_type": "restart",
            "target_resource": "payments-api",
            "target_namespace": "payments",
            "description": "Revert restart",
        },
    }
    proposal = validate_remediation_payload(json.dumps(payload))
    assert proposal.target_resource == "payments-api"
    assert proposal.rollback_action.action_type == "restart"


def test_validate_remediation_payload_missing_rollback():
    payload = {
        "action_type": "restart",
        "target_resource": "payments-api",
        "target_namespace": "payments",
        "reason": "CrashLoopBackOff detected",
    }
    with pytest.raises(ValidationError):
        validate_remediation_payload(json.dumps(payload))


def test_validate_remediation_payload_invalid_json():
    with pytest.raises(ValueError):
        validate_remediation_payload("restart deployment payments-api")


def test_circuit_breaker_cooldown():
    cb = MutationCircuitBreaker(cooldown_seconds=60.0, max_mutations=5)
    cb.check_and_record("payments", "payments-api")

    with pytest.raises(CircuitBreakerError, match="Circuit breaker active"):
        cb.check_and_record("payments", "payments-api")

    # Different resource should succeed
    cb.check_and_record("web", "frontend")


def test_circuit_breaker_budget():
    cb = MutationCircuitBreaker(cooldown_seconds=0.0, max_mutations=2)
    cb.check_and_record("payments", "svc1")
    cb.check_and_record("payments", "svc2")

    with pytest.raises(CircuitBreakerError, match="Incident mutation budget exhausted"):
        cb.check_and_record("payments", "svc3")


def test_k8s_write_trips_circuit_breaker(monkeypatch):
    from common.circuit_breaker import circuit_breaker

    circuit_breaker.reset()
    # First restart passes
    res = k8s_write.restart_deployment("payments-api", "payments")
    assert res["action"] == "restart_deployment"

    # Immediate second restart trips circuit breaker
    with pytest.raises(CircuitBreakerError, match="Circuit breaker active"):
        k8s_write.restart_deployment("payments-api", "payments")

    # Alternating action type on same workload is also blocked by anti-flapping
    with pytest.raises(CircuitBreakerError, match="Circuit breaker active"):
        k8s_write.scale_deployment("payments-api", "payments", 3)


def test_invalid_scale_does_not_trip_circuit_breaker():
    from common.circuit_breaker import circuit_breaker

    circuit_breaker.reset()
    # 999 exceeds MAX_REPLICAS, should raise ValueError without recording in circuit breaker
    with pytest.raises(ValueError):
        k8s_write.scale_deployment("payments-api", "payments", 999)

    # Subsequent valid scale should succeed because circuit breaker was not tripped
    res = k8s_write.scale_deployment("payments-api", "payments", 3)
    assert res["action"] == "scale_deployment"
