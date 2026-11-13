"""Unit tests for Alertmanager webhook ingestion gateway."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from services.gateway import create_gateway_app, deduplicator


def _client():
    return TestClient(create_gateway_app(), base_url="http://localhost")


def test_gateway_health():
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "gateway"}


def test_gateway_non_firing_alert_ignored():
    client = _client()
    payload = {
        "status": "resolved",
        "alerts": [
            {
                "status": "resolved",
                "labels": {"alertname": "PodCrashLooping"},
                "fingerprint": "fp1",
            }
        ],
    }
    response = client.post("/api/v1/alerts", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_gateway_firing_alert_dispatched():
    deduplicator.reset()
    client = _client()
    payload = {
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
                "annotations": {"summary": "Pod payments-api-00 is crashing"},
                "fingerprint": "fp123",
            }
        ],
    }

    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock):
        response = client.post("/api/v1/alerts", json=payload)
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"


def test_gateway_alert_deduplication():
    deduplicator.reset()
    client = _client()
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "PodCrashLooping", "service": "payments-api"},
                "fingerprint": "fp999",
            }
        ],
    }

    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock):
        # First call: accepted
        res1 = client.post("/api/v1/alerts", json=payload)
        assert res1.status_code == 202
        assert res1.json()["status"] == "accepted"

        # Immediate repeat: deduplicated
        res2 = client.post("/api/v1/alerts", json=payload)
        assert res2.status_code == 200
        assert res2.json()["status"] == "deduplicated"


def test_gateway_invalid_payload():
    client = _client()
    response = client.post(
        "/api/v1/alerts", content="invalid json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


def test_gateway_webhook_auth(monkeypatch):
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "super-secret-token")
    client = _client()
    payload = {"status": "firing", "alerts": []}

    # Missing token -> 401
    res1 = client.post("/api/v1/alerts", json=payload)
    assert res1.status_code == 401

    # Valid token -> 200 (ignored because no alerts)
    res2 = client.post(
        "/api/v1/alerts",
        json=payload,
        headers={"Authorization": "Bearer super-secret-token"},
    )
    assert res2.status_code == 200
