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


def _firing_payload(fingerprint="fp-report"):
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "service": "payments-api",
                    "namespace": "payments",
                },
                "annotations": {"summary": "crashing"},
                "fingerprint": fingerprint,
            }
        ],
    }


def test_startup_real_mode_without_token_refuses(monkeypatch):
    import pytest

    from services.gateway import check_startup_config

    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.delenv("WEBHOOK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALERTMANAGER_BEARER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="WEBHOOK_AUTH_TOKEN"):
        check_startup_config()


def test_startup_real_mode_with_token_rejects_wrong_bearer(monkeypatch):
    from services.gateway import check_startup_config

    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "right-token")
    check_startup_config()
    client = _client()
    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock) as dispatch:
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            res = client.post("/api/v1/alerts", json=_firing_payload(), headers=headers)
            assert res.status_code == 401
        dispatch.assert_not_called()


def test_startup_demo_mode_without_token_is_allowed(monkeypatch):
    from services.gateway import check_startup_config

    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.delenv("WEBHOOK_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALERTMANAGER_BEARER_TOKEN", raising=False)
    check_startup_config()


def test_startup_malformed_observer_timeout_refuses(monkeypatch):
    import pytest

    from services.gateway import check_startup_config

    monkeypatch.setenv("OBSERVER_TIMEOUT_S", "ten")
    with pytest.raises(ValueError, match="OBSERVER_TIMEOUT_S"):
        check_startup_config()


def _dispatch(monkeypatch, call_agent):
    import asyncio

    from services import gateway

    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(gateway, "call_agent", call_agent)
    monkeypatch.setattr(gateway, "emit_incident_milestone", notify)
    asyncio.run(
        gateway._dispatch_to_observer(
            "http://observer:9300",
            "prompt",
            incident_id="fp-1",
            service="payments-api",
            namespace="payments",
        )
    )
    return notify


def test_dispatch_honours_observer_timeout_and_publishes_report(monkeypatch):
    monkeypatch.setenv("OBSERVER_TIMEOUT_S", "900")
    call_agent = AsyncMock(return_value="root cause: bad config; recommend restart")
    notify = _dispatch(monkeypatch, call_agent)

    assert call_agent.await_args.kwargs["timeout"] == 900
    card = notify.await_args.kwargs
    assert card["phase"] == "hypothesis"
    assert card["summary"] == "root cause: bad config; recommend restart"
    assert (card["service"], card["namespace"]) == ("payments-api", "payments")


def test_dispatch_failure_is_published(monkeypatch):
    notify = _dispatch(monkeypatch, AsyncMock(side_effect=ConnectionError("observer down")))
    card = notify.await_args.kwargs
    assert card["phase"] == "failed"
    assert "observer down" in card["summary"]
    assert (card["service"], card["namespace"]) == ("payments-api", "payments")


def test_dispatch_timeout_is_published_as_failure(monkeypatch):
    import asyncio

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setenv("OBSERVER_TIMEOUT_S", "0")
    card = _dispatch(monkeypatch, slow).await_args.kwargs
    assert card["phase"] == "failed"
    assert "TimeoutError" in card["summary"]


def test_handler_passes_alert_service_and_namespace_to_dispatch():
    deduplicator.reset()
    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock) as dispatch:
        assert _client().post("/api/v1/alerts", json=_firing_payload("fp-sn")).status_code == 202
    kwargs = dispatch.call_args.kwargs
    assert (kwargs["service"], kwargs["namespace"], kwargs["incident_id"]) == (
        "payments-api",
        "payments",
        "fp-sn",
    )


def test_investigation_prompt_does_not_order_remediation():
    from common.schemas import AlertmanagerPayload
    from services.gateway import _format_investigation_prompt

    for body in (_firing_payload(), {"status": "firing", "alerts": []}):
        prompt = _format_investigation_prompt(AlertmanagerPayload.model_validate(body))
        assert "remediat" not in prompt.lower()
        assert "diagnose" in prompt
    firing = _format_investigation_prompt(AlertmanagerPayload.model_validate(_firing_payload()))
    assert 'source="alertmanager" untrusted="true"' in firing


def test_dedup_window_covers_a_slow_investigation(monkeypatch):
    from services.gateway import DEFAULT_DEDUP_WINDOW_SECONDS, _dedup_window

    monkeypatch.setenv("OBSERVER_TIMEOUT_S", "900")
    assert _dedup_window() == 900
    monkeypatch.setenv("OBSERVER_TIMEOUT_S", "60")
    assert _dedup_window() == DEFAULT_DEDUP_WINDOW_SECONDS


def test_startup_non_positive_observer_timeout_refuses(monkeypatch):
    import pytest

    from services.gateway import check_startup_config

    for value in ("0", "-1"):
        monkeypatch.setenv("OBSERVER_TIMEOUT_S", value)
        with pytest.raises(ValueError, match="OBSERVER_TIMEOUT_S"):
            check_startup_config()


def test_webhook_token_with_trailing_newline_accepts_the_trimmed_bearer(monkeypatch):
    from services.gateway import check_startup_config

    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "right-token\n")
    check_startup_config()
    deduplicator.reset()
    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock):
        res = _client().post(
            "/api/v1/alerts",
            json=_firing_payload("fp-nl"),
            headers={"Authorization": "Bearer right-token"},
        )
    assert res.status_code == 202


def test_card_names_the_alert_that_owns_the_incident(monkeypatch):
    deduplicator.reset()
    first = _firing_payload("fp-old")
    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock):
        _client().post("/api/v1/alerts", json=first)
    grouped = _firing_payload("fp-old")
    grouped["alerts"].append(
        {
            "status": "firing",
            "labels": {"alertname": "HighLatency", "service": "checkout", "namespace": "shop"},
            "fingerprint": "fp-new",
        }
    )
    with patch("services.gateway._dispatch_to_observer", new_callable=AsyncMock) as dispatch:
        assert _client().post("/api/v1/alerts", json=grouped).status_code == 202
    kwargs = dispatch.call_args.kwargs
    assert (kwargs["incident_id"], kwargs["service"], kwargs["namespace"]) == (
        "fp-new",
        "checkout",
        "shop",
    )
